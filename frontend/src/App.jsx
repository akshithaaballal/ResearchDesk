import { useEffect, useRef, useState } from "react";
import "./App.css";

const API_URL = "http://127.0.0.1:8000/research";
const STORAGE_KEY = "researchChats";
const MAX_CHATS = 50;

const Icon = ({ children }) => (
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.8"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    {children}
  </svg>
);

const PlusIcon = () => (
  <Icon>
    <path d="M12 5v14M5 12h14" />
  </Icon>
);
const MenuIcon = () => (
  <Icon>
    <path d="M4 7h16M4 12h16M4 17h16" />
  </Icon>
);
const CloseIcon = () => (
  <Icon>
    <path d="M6 6l12 12M18 6L6 18" />
  </Icon>
);
const TrashIcon = () => (
  <Icon>
    <path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-12M9 7V4h6v3" />
  </Icon>
);
const SendIcon = () => (
  <Icon>
    <path d="M12 19V5M5 12l7-7 7 7" />
  </Icon>
);
const CopyIcon = () => (
  <Icon>
    <rect x="9" y="9" width="11" height="11" rx="2" />
    <path d="M5 15V6a2 2 0 0 1 2-2h9" />
  </Icon>
);
const CheckIcon = () => (
  <Icon>
    <path d="M5 12l5 5 9-10" />
  </Icon>
);

const newId = () =>
  typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID()
    : String(Date.now() + Math.random());

function loadChats() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY));
    if (Array.isArray(saved)) return saved;

    // Migrate the previous single-turn history format
    const old = JSON.parse(localStorage.getItem("researchHistory"));
    if (Array.isArray(old)) {
      return old.map((item) => ({
        id: newId(),
        title: item.result?.title || item.input,
        messages: [
          { id: newId(), role: "user", text: item.input },
          { id: newId(), role: "assistant", result: item.result },
        ],
      }));
    }
  } catch (error) {
    console.error(error);
  }
  return [];
}

function resultToText(result) {
  const concepts = (result.key_concepts || []).map((c) => `- ${c}`).join("\n");
  const findings = (result.key_findings || []).map((f) => `- ${f}`).join("\n");
  return `${result.title}\n\nOverview\n${result.overview}\n\nKey Concepts\n${concepts}\n\nKey Findings\n${findings}`;
}

function CopyButton({ result }) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(resultToText(result));
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch (error) {
      console.error(error);
    }
  };

  return (
    <button
      className="icon-btn"
      onClick={copy}
      aria-label={copied ? "Copied" : "Copy"}
      title={copied ? "Copied" : "Copy"}
    >
      {copied ? <CheckIcon /> : <CopyIcon />}
    </button>
  );
}

function Result({ result }) {
  return (
    <div className="result">
      <h2>{result?.title || "Research Result"}</h2>

      <section className="result-section">
        <h3>Overview</h3>
        <p style={{ color: "#ffffff" }}>
          {result?.overview || "No overview available"}
        </p>
      </section>

      <section className="result-section">
        <h3>Key Concepts</h3>
        <ul>
          {(result?.key_concepts || []).map((concept, index) => (
            <li key={index} style={{ color: "#ffffff" }}>
              {concept}
            </li>
          ))}
        </ul>
      </section>

      <section className="result-section">
        <h3>Key Findings</h3>
        <ul>
          {(result?.key_findings || []).map((finding, index) => (
            <li key={index} style={{ color: "#ffffff" }}>
              {finding}
            </li>
          ))}
        </ul>
      </section>

      <div className="message-actions">
        <CopyButton result={result} />
      </div>
    </div>
  );
}

function App() {
  const [chats, setChats] = useState(loadChats);
  const [activeId, setActiveId] = useState(null);
  const [input, setInput] = useState("");
  const [loadingId, setLoadingId] = useState(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);

  // Ask Docs state
  const [mode, setMode] = useState("research"); // 'research' | 'askdocs'
  const [askDocsToken, setAskDocsToken] = useState(() => localStorage.getItem("askdocs_token") || "");
  const [uploadedDocId, setUploadedDocId] = useState(null);
  const [uploading, setUploading] = useState(false);

  const threadRef = useRef(null);
  const textareaRef = useRef(null);
  const fileInputRef = useRef(null);

  const activeChat = chats.find((chat) => chat.id === activeId) || null;
  const messages = activeChat ? activeChat.messages : [];
  const isLoading = loadingId !== null && loadingId === activeId;

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(chats));
    } catch (error) {
      console.error(error);
    }
  }, [chats]);

  useEffect(() => {
    localStorage.setItem("askdocs_token", askDocsToken);
  }, [askDocsToken]);

  useEffect(() => {
    const el = threadRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length, isLoading, activeId]);

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [input]);

  const startNewChat = () => {
    setActiveId(null);
    setInput("");
    setSidebarOpen(false);
    textareaRef.current?.focus();
  };

  const selectChat = (id) => {
    setActiveId(id);
    setSidebarOpen(false);
  };

  const deleteChat = (id) => {
    setChats((prev) => prev.filter((chat) => chat.id !== id));
    if (activeId === id) setActiveId(null);
  };

  const appendMessage = (chatId, message) => {
    setChats((prev) =>
      prev.map((chat) =>
        chat.id === chatId
          ? { ...chat, messages: [...chat.messages, message] }
          : chat
      )
    );
  };
  const handleFileUpload = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    if (!askDocsToken) {
      alert("Please enter your Ask Docs token in the sidebar first.");
      return;
    }

    setUploading(true);
    try {
      const formData = new FormData();
      formData.append("file", file);
      formData.append("token", askDocsToken);

      const res = await fetch("http://127.0.0.1:8000/askdocs/upload", {
        method: "POST",
        body: formData,
      });

      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || "Upload failed");
      }
      const data = await res.json();
      console.log("Upload response:", data);
      
      const docId = data.document_id || data.doc_id || data.id || data.file_id;
      if (docId) {
        setUploadedDocId(docId);
        alert("Document uploaded successfully! You can now ask questions about it.");
      } else {
        // As a fallback, if no clear ID is found, just store the fact we uploaded something
        setUploadedDocId("uploaded");
        alert("Document uploaded successfully!");
      }
    } catch (error) {
      console.error(error);
      alert("Upload failed: " + error.message);
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const send = async () => {
    const text = input.trim();
    if (!text || loadingId !== null) return;

    if (mode === "askdocs" && !askDocsToken) {
      alert("Please enter your Ask Docs token in the sidebar first.");
      return;
    }

    let chatId = activeId;
    const userMessage = { id: newId(), role: "user", text };

    if (!chatId) {
      chatId = newId();
      setChats((prev) =>
        [{ id: chatId, title: text, messages: [userMessage] }, ...prev].slice(
          0,
          MAX_CHATS
        )
      );
      setActiveId(chatId);
    } else {
      appendMessage(chatId, userMessage);
    }

    setInput("");
    setLoadingId(chatId);

    try {
      let response;
      if (mode === "research") {
        response = await fetch(API_URL, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ input: text }),
        });
      } else {
        response = await fetch("http://127.0.0.1:8000/askdocs/ask", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ 
            question: text, 
            token: askDocsToken,
            doc_id: uploadedDocId
          }),
        });
      }

      if (!response.ok) {
        let detail = "Request failed";
        try {
          const err = await response.json();
          if (typeof err?.detail === "string") detail = err.detail;
        } catch {}
        throw new Error(detail);
      }

      const data = await response.json();
      console.log("API RESPONSE:", data);

      if (mode === "research") {
        const hasContent = data && (data.overview || (data.key_concepts || []).length || (data.key_findings || []).length);
        if (!data || data.status === "error" || data.status === "timeout" || !hasContent) {
          throw new Error(data?.message || "The research agent returned no content. Please try again.");
        }
        setChats((prev) =>
          prev.map((chat) =>
            chat.id === chatId
              ? {
                  ...chat,
                  title: chat.messages.length === 1 && data.title ? data.title : chat.title,
                  messages: [...chat.messages, { id: newId(), role: "assistant", result: data }],
                }
              : chat
          )
        );
      } else {
        // Ask Docs response is likely { answer: "..." } or plain text structure
        const answerText = data.answer || data.response || data.text || JSON.stringify(data);
        setChats((prev) =>
          prev.map((chat) =>
            chat.id === chatId
              ? {
                  ...chat,
                  messages: [...chat.messages, { id: newId(), role: "assistant-text", text: answerText }],
                }
              : chat
          )
        );
      }
    } catch (error) {
      console.error(error);
      appendMessage(chatId, {
        id: newId(),
        role: "error",
        text:
          error instanceof TypeError
            ? "Could not connect to the backend."
            : error.message || "Something went wrong.",
      });
    } finally {
      setLoadingId(null);
    }
  };

  const onKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      send();
    }
  };

  const composer = (
    <div className="composer">
      <div className="composer-inner">
        {mode === "askdocs" && (
          <div style={{ padding: "8px", fontSize: "12px", color: "var(--text-dim)" }}>
            <input 
              type="file" 
              ref={fileInputRef}
              onChange={handleFileUpload} 
              style={{ display: 'none' }}
              accept=".pdf,.csv,.xlsx,.docx,.txt"
            />
            {uploadedDocId ? (
              <span>Attached Doc ID: {uploadedDocId}</span>
            ) : (
              <span>No document attached. </span>
            )}
            <button 
              className="outline-btn"
              style={{ marginLeft: '8px', padding: '4px 8px', fontSize: '11px', cursor: 'pointer' }}
              onClick={() => fileInputRef.current?.click()}
              disabled={uploading}
            >
              {uploading ? "Uploading..." : "Upload Document"}
            </button>
          </div>
        )}
        <div className="composer-box">
          <textarea
            ref={textareaRef}
            rows={1}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder={mode === "askdocs" ? "Ask questions about your documents..." : "Ask anything"}
            aria-label="Prompt input"
          />
          <button
            className="send"
            onClick={send}
            disabled={!input.trim() || loadingId !== null}
            aria-label="Send"
          >
            <SendIcon />
          </button>
        </div>
      </div>
    </div>
  );

  return (
    <div className="app">
      <aside className={`sidebar ${sidebarOpen ? "open" : ""}`}>
        <div className="sidebar-top">
          <span className="brand">ResearchDesk</span>
          <button
            className="icon-btn close-btn"
            onClick={() => setSidebarOpen(false)}
            aria-label="Close sidebar"
          >
            <CloseIcon />
          </button>
        </div>

        <div style={{ padding: "16px", borderBottom: "1px solid var(--border)", display: "flex", flexDirection: "column", gap: "12px" }}>
          <label style={{ fontSize: "14px", fontWeight: 500 }}>Mode</label>
          <select 
            value={mode} 
            onChange={(e) => setMode(e.target.value)}
            style={{ padding: "8px", borderRadius: "6px", background: "var(--bg)", color: "var(--text)", border: "1px solid var(--border)" }}
          >
            <option value="research">Gemini Research</option>
            <option value="askdocs">Cosmitude Ask Docs</option>
          </select>

          {mode === "askdocs" && (
            <>
              <label style={{ fontSize: "14px", fontWeight: 500 }}>Ask Docs Token</label>
              <input
                type="password"
                placeholder="Enter Bearer token"
                value={askDocsToken}
                onChange={(e) => setAskDocsToken(e.target.value)}
                style={{ padding: "8px", borderRadius: "6px", background: "var(--bg)", color: "var(--text)", border: "1px solid var(--border)", width: "100%" }}
              />
            </>
          )}
        </div>

        <button className="new-chat" onClick={startNewChat} style={{ marginTop: "16px" }}>
          <PlusIcon />
          New chat
        </button>

        <nav className="chat-list" aria-label="Research history">
          {chats.map((chat) => (
            <div
              key={chat.id}
              className={`chat-item ${chat.id === activeId ? "active" : ""}`}
            >
              <button
                className="chat-select"
                onClick={() => selectChat(chat.id)}
                title={chat.title}
              >
                {chat.title}
              </button>
              <button
                className="icon-btn chat-delete"
                onClick={() => deleteChat(chat.id)}
                aria-label={`Delete ${chat.title}`}
                title="Delete"
              >
                <TrashIcon />
              </button>
            </div>
          ))}
        </nav>
      </aside>

      <div
        className={`backdrop ${sidebarOpen ? "show" : ""}`}
        onClick={() => setSidebarOpen(false)}
      />

      <main className="main">
        <header className="topbar">
          <button
            className="icon-btn menu-btn"
            onClick={() => setSidebarOpen(true)}
            aria-label="Open sidebar"
          >
            <MenuIcon />
          </button>
          <span className="topbar-title">
            {activeChat ? activeChat.title : "ResearchDesk"}
          </span>
        </header>

        {activeChat ? (
          <>
            <div className="thread" ref={threadRef}>
              <div className="thread-inner">
                {messages.map((message) => {
                  if (message.role === "user") {
                    return (
                      <div key={message.id} className="message-user">
                        {message.text}
                      </div>
                    );
                  }
                  if (message.role === "error") {
                    return (
                      <div key={message.id} className="message-error">
                        {message.text}
                      </div>
                    );
                  }
                  if (message.role === "assistant-text") {
                    return (
                      <div key={message.id} className="result-card" style={{ padding: '16px' }}>
                        <div style={{ whiteSpace: 'pre-wrap', lineHeight: '1.6' }}>{message.text}</div>
                      </div>
                    );
                  }
                  return <Result key={message.id} result={message.result} />;
                })}

                {isLoading && (
                  <div className="typing" aria-label="Generating">
                    <span />
                    <span />
                    <span />
                  </div>
                )}
              </div>
            </div>
            {composer}
          </>
        ) : (
          <>
            <div className="empty">
              <h1>{mode === "askdocs" ? "What do you want to ask your docs?" : "What do you want to research?"}</h1>
            </div>
            {composer}
          </>
        )}
      </main>
    </div>
  );
}

export default App;

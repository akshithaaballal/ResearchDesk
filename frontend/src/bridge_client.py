class BridgeClient:
    def __init__(self):
        self.connected = False
        self.latest_request = None
        self.latest_result = None

    def send_research_request(self, research_input):
        self.latest_request = {
            "input": research_input
        }

        print("Bridge request:", self.latest_request)

        return {
            "status": "received",
            "input": research_input
        }


bridge = BridgeClient()



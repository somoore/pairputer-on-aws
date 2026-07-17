class BrowserEffector:
    def __init__(self, service): self.service = service
    def open(self, request): return self.service.open(request)
    def action(self, request): return self.service.action(request)

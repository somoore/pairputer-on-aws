class AppEffector:
    def __init__(self, service): self.service = service
    def open(self, request): return self.service.open(request)
    def focus_window(self, request): return self.service.focus_window(request)

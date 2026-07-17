class ProcessEffector:
    def __init__(self, service): self.service = service
    def start(self, request): return self.service.start(request)
    def cancel(self, request): return self.service.cancel(request)

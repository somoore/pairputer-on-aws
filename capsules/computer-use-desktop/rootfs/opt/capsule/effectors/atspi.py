class AtspiEffector:
    def __init__(self, service): self.service = service
    def action(self, request): return self.service.action(request)

class WorkspaceEffector:
    def __init__(self, service): self.service = service
    def write(self, request): return self.service.write(request)
    def patch(self, request): return self.service.patch(request)
    def move(self, request): return self.service.move(request)
    def trash(self, request): return self.service.trash(request)

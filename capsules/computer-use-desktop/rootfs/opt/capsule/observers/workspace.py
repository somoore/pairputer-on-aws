class WorkspaceObserver:
    def __init__(self, service): self.service = service
    def list(self, *args, **kwargs): return self.service.list(*args, **kwargs)
    def describe(self, *args, **kwargs): return self.service.describe(*args, **kwargs)
    def read(self, *args, **kwargs): return self.service.read(*args, **kwargs)

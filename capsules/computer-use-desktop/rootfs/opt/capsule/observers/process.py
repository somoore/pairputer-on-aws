class ProcessObserver:
    def __init__(self, service): self.service = service
    def status(self, *args, **kwargs): return self.service.status(*args, **kwargs)

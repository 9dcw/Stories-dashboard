"""API-facing facade; execution belongs to the separately supervised worker."""
from phase2.store import WorkflowStore


class Phase2Operations:
    def __init__(self,store):
        self.store=WorkflowStore(store.db_path)
        self.store.initialize()

    def enqueue(self,story_id,retry=False):
        if retry:
            return self.store.retry(story_id)
        return self.store.enqueue(story_id,'dashboard:initial:'+str(story_id))

    def public(self,story_id):
        return self.store.status(story_id)

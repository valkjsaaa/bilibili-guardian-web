class Config:
    user: int
    video_count: int
    dynamic_count: int
    max_page: int
    url: str

    def __init__(self, user=941228, video_count=20, dynamic_count=20, max_page=10):
        self.user = user
        self.video_count = video_count
        self.dynamic_count = dynamic_count
        self.max_page = max_page

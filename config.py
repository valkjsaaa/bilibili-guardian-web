from bilibili_api import Credential

from bilibili import Bilibili


class Config:
    user: int
    video_count: int
    dynamic_count: int
    max_page: int
    url: str

    def __init__(self, user=941228, video_count=200, dynamic_count=300, max_page=10, username=None, password=None):
        self.user = user
        self.video_count = video_count
        self.dynamic_count = dynamic_count
        self.max_page = max_page
        self.username = username
        self.password = password
        self.credential = None
        if username is not None and password is not None:
            b = Bilibili()
            b.login(username=username, password=password)
            self.credential = Credential(
                sessdata=b._session.cookies['SESSDATA'],
                bili_jct=b._session.cookies['bili_jct'],
                buvid3="6FEFA119-C949-48A2-9D7C-320155B3460E167612infoc"
            )

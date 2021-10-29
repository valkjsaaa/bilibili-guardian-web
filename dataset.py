import json
from datetime import datetime

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Column, Integer, DateTime, Text

db = SQLAlchemy()


class Comment(db.Model):
    __tablename__ = 'comment'
    rpid = Column(Integer, primary_key=True)  # 回复 ID
    message = Column(Text)  # 回复文本
    oid = Column(Integer)  # 回复内容 ID
    oname = Column(Integer)  # 回复内容标题
    type_ = Column(Integer)  # 回复内容类型
    mid = Column(Integer)  # 回复用户 ID
    mname = Column(Integer)  # 回复用户昵称
    fansgrade = Column(Integer)  # 是否为粉丝
    ctime = Column(DateTime)  # 发布时间
    rcount = Column(Integer)  # 回复数目
    like = Column(Integer)  # 点赞数目
    guardian_status = Column(Integer)  # 守护状态
    raw = Column(Text)  # 原始 JSON

    def type_name(self):
        if self.type_ == 1:
            return "视频"
        elif self.type_ == 12:
            return "动态"
        else:
            return "未知"

    def object_desc(self):
        if self.type_ == 1:
            return f"{self.type_name()} av{self.oid}"
        elif self.type == 12:
            return f"{self.type_name()} {self.oid}"

    @staticmethod
    def get_link(type_: int, oid: int, rpid: int) -> str:
        if type_ == 1:
            return f"https://www.bilibili.com/video/av{oid}#reply{rpid}"
        else:
            return f"https://t.bilibili.com/{oid}#reply{rpid}"

    @staticmethod
    def get_object_link(type_: int, oid: int, rpid: int) -> str:
        if type_ == 1:
            return f"https://www.bilibili.com/video/av{oid}"
        else:
            return f"https://t.bilibili.com/{oid}"

    @staticmethod
    def abstract_text(text: str, length: int) -> str:
        if len(text) < length:
            return text
        else:
            return text[:length] + "..."

    def __repr__(self):
        return f"在{self.object_desc()}下用户 {self.mname} 的评论 {self.abstract_text(self.message, 10)}"

    def __init__(self, user_json: {}, oname):
        self.rpid = user_json['rpid']
        self.message = user_json['content']['message']
        self.oid = user_json['oid']
        self.oname = oname
        self.type_ = user_json['type']
        self.mid = user_json['mid']
        self.mname = user_json['member']['uname']
        self.fansgrade = user_json['fansgrade']
        self.ctime = datetime.utcfromtimestamp(user_json['ctime'])
        self.rcount = user_json['rcount']
        self.like = user_json['like']
        self.guardian_status = 0
        self.raw = json.dumps(user_json)

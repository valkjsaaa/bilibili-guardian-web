import asyncio
import sys
import threading
import traceback

import aiohttp.client_exceptions
from bilibili_api import user, comment
from bilibili_api.comment import ResourceType
from flask import Flask
from flask_sqlalchemy import SQLAlchemy

from config import Config
from dataset import Comment


async def retries(f, times=5):
    for i in range(times):
        try:
            return await f()
        except aiohttp.client_exceptions.ServerDisconnectedError:
            print(f"服务器端开链接，重试第{i}次...")


class Scraper:
    def __init__(self, config: Config, db: SQLAlchemy, app: Flask):
        self.config = config
        self.db = db
        self.app = app

    async def scrap(self):
        user_obj = user.User(self.config.user)

        user_info = await retries(lambda: user_obj.get_user_info())
        print(f"载入用户：{user_info['name']}")

        videos = []
        pn = 1
        while True:
            current_videos_result = await retries(lambda: user_obj.get_videos(pn=pn))
            if len(current_videos_result['list']['vlist']) == 0:
                break
            current_videos = current_videos_result['list']['vlist']
            filtered_current_videos = [video for video in current_videos if video['mid'] == self.config.user]
            videos += filtered_current_videos
            if len(videos) >= self.config.video_count:
                break
            pn += 1

        recent_videos = videos[:min(self.config.video_count, len(videos))]

        for video in recent_videos:
            video_comments = {}
            for i in range(self.config.max_page):
                new_comments_result = \
                    await retries(lambda:
                                  comment.get_comments(video["aid"], type_=comment.ResourceType.VIDEO, page_index=i + 1)
                                  )
                new_comments = new_comments_result['replies']
                filtered_new_comments = [
                    comment_
                    for comment_ in new_comments
                    if Comment.query.get(comment_['rpid']) is None
                ]
                if len(filtered_new_comments) == 0:  # last page or all comments are covered in database
                    break
                for comment_ in filtered_new_comments:
                    video_comments[comment_['rpid']] = comment_
            db_comments = [Comment(comment_, video['title']) for comment_ in video_comments.values()]
            self.db.session.bulk_save_objects(db_comments)
            self.db.session.commit()

        dynamics = []
        pn = 1
        offset = 0
        while True:
            current_dynamics_result = await retries(lambda: user_obj.get_dynamics(offset=offset))
            if len(current_dynamics_result['cards']) == 0:
                break
            current_dynamics = current_dynamics_result['cards']
            filtered_current_dynamics = [
                dynamic_
                for dynamic_ in current_dynamics
                if dynamic_['desc']['type'] != 8
            ]
            dynamics += filtered_current_dynamics
            if len(dynamics) >= self.config.dynamic_count:
                break
            pn += 1

        def dynamic_desc(dynamic_: {}) -> str:
            dynamic_type = dynamic_['desc']['type']
            # dynamic_type 见 https://github.com/SocialSisterYi/bilibili-API-collect/issues/143
            if dynamic_type == 1:
                return f"{dynamic_['card']['item']['content']}：转发\"{dynamic_['card']['origin_user']['info']['uname']}\""
            elif dynamic_type == 2:
                return dynamic_['card']['item']['description']
            elif dynamic_type == 4:
                return dynamic_['card']['item']['content']

        def dynamic_resource_type(dynamic_: {}):
            dynamic_type = dynamic_['desc']['type']
            if dynamic_type == 2:
                return ResourceType.DYNAMIC_DRAW
            else:
                return ResourceType.DYNAMIC

        def dynamic_oid(dynamic_: {}):
            dynamic_type = dynamic_['desc']['type']
            if dynamic_type == 2:
                return dynamic["desc"]['rid']
            else:
                return dynamic["desc"]['dynamic_id']

        recent_dynamics = dynamics[:min(self.config.dynamic_count, len(dynamics))]

        for dynamic in recent_dynamics:
            dynamic_comments = {}
            for i in range(self.config.max_page):
                new_comments_result = \
                    await retries(lambda:
                                  comment.get_comments(dynamic_oid(dynamic),
                                                       type_=dynamic_resource_type(dynamic),
                                                       page_index=i + 1)
                                  )
                new_comments = new_comments_result['replies']
                filtered_new_comments = [
                    comment_
                    for comment_ in new_comments
                    if Comment.query.get(comment_['rpid']) is None
                ]
                if len(filtered_new_comments) == 0:  # last page or all comments are covered in database
                    break
                for comment_ in filtered_new_comments:
                    dynamic_comments[comment_['rpid']] = comment_
            db_comments = [Comment(comment_, dynamic_desc(dynamic)) for comment_ in dynamic_comments.values()]
            self.db.session.bulk_save_objects(db_comments)
            self.db.session.commit()

    async def scraper_loop(self):
        self.app.app_context().push()
        while True:
            # try:
            await self.scrap()
            # except Exception as err:
            #     print(f"Unknown posting exception: {err}")
            #     print(traceback.format_exc())
            # finally:
            #     sys.stdout.flush()

    def scraper_thread(self, loop):
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.scraper_loop())

    def run_scraper(self):
        loop = asyncio.get_event_loop()
        thread = threading.Thread(target=self.scraper_thread, args=(loop,))
        thread.start()

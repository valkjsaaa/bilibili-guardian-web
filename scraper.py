import asyncio
import sys
import threading
import traceback
from datetime import datetime, timedelta
from queue import Queue
from typing import Optional

import aiohttp.client_exceptions
import tqdm as tqdm
from bilibili_api import user, comment, exceptions
from bilibili_api.comment import ResourceType, OrderType
from flask import Flask
from flask_sqlalchemy import SQLAlchemy

from config import Config
from dataset import Comment


async def retries(f, times=5):
    for i in range(times):
        try:
            return await f()
        except aiohttp.client_exceptions.ServerDisconnectedError:
            print(f"服务器端开链接，重试第{i + 1}次...")
        except asyncio.exceptions.TimeoutError:
            print(f"服务器超时，重试第{i + 1}次...")
        except exceptions.NetworkException:
            print(f"接口被屏蔽，重试第{i + 1}次...")
            await asyncio.sleep(120 * (2 ** i))


class Scraper:
    def __init__(self, config: Config, db: SQLAlchemy, app: Flask):
        self.config = config
        self.db = db
        self.app = app
        self.last_refreshed = None
        self.refresh_queue = Queue()

        self.last_block: Optional[datetime] = None
        self.wait_time = 0
        self.wait_level = 0
        self.first_trial = False

    async def allow_blocked(self, f):
        current_time = datetime.now()
        if self.last_block is None or current_time - self.last_block < timedelta(seconds=self.wait_time):
            try:
                result = await f()
                if self.first_trial:
                    self.wait_level -= 1
                    if self.wait_level < 0:
                        self.wait_level = 0
                self.first_trial = False
                return result
            except exceptions.NetworkException:
                if self.first_trial:
                    self.wait_level += 1
                wait_time = 150 * (2 ** self.wait_level)
                self.first_trial = True
                self.last_block = current_time
                print(f"接口被屏蔽，等待{wait_time}秒")
                return None
        else:
            return None

    async def scrap(self):
        user_obj = user.User(self.config.user, credential=self.config.credential)

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

        async def get_comments(
                oid: int,
                type_: comment.ResourceType,
                max_page: int,
                order: OrderType,
                ignore_list: {int} = None
        ) -> ({int: dict}, {int: [int]}):
            full_scrape = False
            if ignore_list is None:
                ignore_list = {}
            comments_dict = {}
            sub_comments_dict = {}
            for i in range(max_page):
                new_comments_result = \
                    await retries(lambda:
                                  comment.get_comments(oid, type_=type_, page_index=i + 1, order=order,
                                                       credential=self.config.credential)
                                  )
                new_comments = new_comments_result['replies']
                for comment_ in new_comments:
                    if comment_['rpid'] in ignore_list:
                        continue
                    comments_dict[comment_['rpid']] = comment_
                    sub_comment_ids = []
                    if comment_['rcount'] > len(comment_['replies']):
                        comment_bilibili = comment.Comment(comment_['oid'], ResourceType(comment_['type']),
                                                           comment_['rpid'], credential=self.config.credential)
                        j = 1
                        while True:
                            sub_comments = await retries(
                                lambda: self.allow_blocked(
                                    lambda: comment_bilibili.get_sub_comments(page_index=j)
                                )
                            )
                            if sub_comments is None:
                                sub_comment_ids = []
                                break
                            if sub_comments['replies'] is None:
                                break
                            for sub_comment in sub_comments['replies']:
                                comments_dict[sub_comment['rpid']] = sub_comment
                                sub_comment_ids += [sub_comment['rpid']]
                            j += 1
                    else:
                        for sub_comment in comment_['replies']:
                            comments_dict[sub_comment['rpid']] = sub_comment
                            sub_comment_ids += [sub_comment['rpid']]
                    if len(sub_comment_ids) > 0:
                        sub_comments_dict[comment_['rpid']] = sub_comment_ids

                if len(new_comments) == 0:
                    full_scrape = True
                    break
            return comments_dict, sub_comments_dict, full_scrape

        def update_comments(oname: str, oid: int, comments_time: [], comments_like: [], full_scrape=False):
            if full_scrape:
                db_comments = [Comment(comment_, oname) for comment_ in comments_time.values()]
                db_comments += [Comment(comment_, oname) for comment_ in comments_like.values()]
                earliest_time = min([comment_.ctime for comment_ in db_comments if comment_.root == 0])
            else:
                db_comments = [Comment(comment_, oname) for comment_ in comments_time.values()]
                earliest_time = min([comment_.ctime for comment_ in db_comments if comment_.root == 0])
                db_comments += [Comment(comment_, oname) for comment_ in comments_like.values()]
            later_comments = Comment.query.filter(
                Comment.ctime >= earliest_time,
                Comment.oid == oid,
                Comment.root == 0
            ).all()
            for later_comment in later_comments:
                if later_comment.rpid not in all_rpid:
                    later_comment.guardian_status = -1
                    sub_comments = Comment.query.filter(
                        Comment.root == later_comment.rpid
                    ).all()
                    for comment_ in sub_comments:
                        comment_.guardian_status = -1
                else:
                    later_comment.guardian_status = 1

            for sub_comment_rpid, sub_comment_sub_comments_rpids in sub_comments_dict.items():
                sub_comments = Comment.query.filter(
                    Comment.root == sub_comment_rpid
                ).all()
                for sub_comment in sub_comments:
                    if sub_comment.rpid not in sub_comment_sub_comments_rpids:
                        sub_comment.guardian_status = -1
                    else:
                        sub_comment.guardian_status = 1

            filtered_db_comments = [comment_ for comment_ in db_comments if Comment.query.get(comment_.rpid) is None]
            self.db.session.bulk_save_objects(filtered_db_comments)
            self.db.session.commit()

        for video in tqdm.tqdm(recent_videos):
            video_comments_time, video_sub_comments_time, full_scrape_time = await get_comments(
                video["aid"],
                type_=comment.ResourceType.VIDEO,
                max_page=self.config.max_page,
                order=OrderType.TIME
            )
            video_comments_likes, video_sub_comments_likes, full_scrape_like = await get_comments(
                video["aid"],
                type_=comment.ResourceType.VIDEO,
                max_page=self.config.max_page,
                order=OrderType.LIKE,
                ignore_list=set(video_comments_time.keys())
            )
            all_rpid = set(video_comments_likes.keys()).union(video_comments_time.keys())
            sub_comments_dict = dict(video_sub_comments_time)
            sub_comments_dict.update(video_sub_comments_likes)

            update_comments(
                video['title'],
                video['aid'],
                video_comments_time,
                video_comments_likes,
                full_scrape_time and full_scrape_like
            )

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

        for dynamic in tqdm.tqdm(recent_dynamics):
            dynamic_comments_time, dynamic_sub_comments_time, full_scrape_time = await get_comments(
                dynamic_oid(dynamic),
                type_=dynamic_resource_type(dynamic),
                max_page=self.config.max_page,
                order=OrderType.TIME
            )
            dynamic_comments_likes, dynamic_sub_comments_likes, full_scrape_like = await get_comments(
                dynamic_oid(dynamic),
                type_=dynamic_resource_type(dynamic),
                max_page=self.config.max_page,
                order=OrderType.LIKE,
                ignore_list=set(dynamic_comments_time.keys())
            )
            all_rpid = set(dynamic_comments_likes.keys()).union(dynamic_comments_time.keys())
            sub_comments_dict = dict(dynamic_sub_comments_time)
            sub_comments_dict.update(dynamic_sub_comments_likes)

            update_comments(
                dynamic_desc(dynamic),
                dynamic_oid(dynamic),
                dynamic_comments_time,
                dynamic_comments_likes,
                full_scrape_like and full_scrape_time
            )
        self.last_refreshed = datetime.now()

    async def scraper_loop(self):
        self.app.app_context().push()
        while True:
            try:
                await self.scrap()
            except Exception as err:
                print(f"Unknown posting exception: {err}")
                print(traceback.format_exc())
            finally:
                sys.stdout.flush()

    def scraper_thread(self, loop):
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.scraper_loop())

    def run_scraper(self):
        loop = asyncio.get_event_loop()
        thread = threading.Thread(target=self.scraper_thread, args=(loop,))
        thread.start()

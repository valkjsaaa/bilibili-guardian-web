import asyncio
import sys
import threading
import traceback
from datetime import datetime, timedelta
from queue import Queue
from typing import Optional

import aiohttp.client_exceptions
import tqdm as tqdm
from bilibili_api import user, comment, exceptions, video
from bilibili_api.comment import CommentResourceType, OrderType
from flask import Flask
from flask_sqlalchemy import SQLAlchemy

from config import Config
from dataset import Comment

DISPLAY_BEFORE_TIMESTAMP = 1636611395

async def retries(f, times=5):
    for i in range(times):
        try:
            await asyncio.sleep(1)
            return await f()
        except aiohttp.client_exceptions.ServerDisconnectedError:
            print(f"服务器端开链接，重试第{i + 1}次...")
        except asyncio.exceptions.TimeoutError:
            print(f"服务器超时，重试第{i + 1}次...")
        except exceptions.ApiException as e:
            if isinstance(e, exceptions.NetworkException):
                print(f"接口被屏蔽，重试第{i + 1}次...")
                await asyncio.sleep(120 * (2 ** i))
            elif isinstance(e, exceptions.ResponseCodeException):
                if e.code == -404:
                    raise e
                else:
                    print(f"错误代码{e.code}，重试第{i + 1}次...")
            else:
                print(f"API异常：{e}，重试第{i + 1}次...")


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

        self.new_video_oids = []
        self.new_dynamic_oids = []
        
        # 爬虫处理速率统计 - 改进版
        self.scraper_stats = {
            'comment_rate': 0,      # 每秒处理评论数
            'video_rate': 0,        # 每分钟处理视频数
        }
        # 记录每批评论数量和时间戳: [(count, timestamp), ...]
        self.comment_records = [(0, datetime.now())]  # 初始占位记录
        # 记录每批视频数量和时间戳: [(count, timestamp), ...]
        self.video_records = [(0, datetime.now())]    # 初始占位记录

        self.recent_videos = []  # Store timestamps for recent video discoveries
        self.recent_comments = []  # Store timestamps for recent comment discoveries

    # Add a helper method to track new comments
    def track_new_comment(self):
        """Track a new comment for rate calculations"""
        now = datetime.now()
        self.recent_comments.append(now)
        # Keep only last 30 minutes of data
        self.recent_comments = [t for t in self.recent_comments if now - t < timedelta(minutes=30)]

    # Add a helper method to track new videos
    def track_new_video(self):
        """Track a new video for rate calculations"""
        now = datetime.now()
        self.recent_videos.append(now)
        # Keep only last 30 minutes of data
        self.recent_videos = [t for t in self.recent_videos if now - t < timedelta(minutes=30)]

    async def allow_blocked(self, f):
        current_time = datetime.now()
        if self.last_block is None or current_time - self.last_block < timedelta(seconds=self.wait_time):
            try:
                await asyncio.sleep(1)
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
        print("开始抓取")
        
        # 刷新统计记录，添加新的起始记录点
        now = datetime.now()
        self.comment_records.append((0, now))
        self.video_records.append((0, now))
        
        # 清理旧记录
        cutoff = now - timedelta(minutes=30)
        self.comment_records = [r for r in self.comment_records if r[1] >= cutoff]
        self.video_records = [r for r in self.video_records if r[1] >= cutoff]
        
        # Initialize tracking for current scrape session
        if not hasattr(self, 'recent_comments'):
            self.recent_comments = []
        if not hasattr(self, 'recent_videos'):
            self.recent_videos = []
        
        user_obj = user.User(self.config.user, credential=self.config.credential)

        user_info = await retries(lambda: user_obj.get_user_info())
        print(f"载入用户：{user_info['name']}")

        # Get user videos
        videos_list = []
        page = 1
        while True:
            video_pagination = await retries(lambda: user_obj.get_videos(pn=page))
            if not video_pagination['list']['vlist']:
                break
            
            current_videos = video_pagination['list']['vlist']
            filtered_current_videos = [v for v in current_videos if v['mid'] == self.config.user]
            videos_list.extend(filtered_current_videos)
            
            if len(videos_list) >= self.config.video_count:
                break
            page += 1

        recent_videos = videos_list[:min(self.config.video_count, len(videos_list))]
        self.new_video_oids = [v["aid"] for v in videos_list if v.get('created', 0) > DISPLAY_BEFORE_TIMESTAMP]

        # Get user dynamics
        dynamics_list = []
        offset = 0
        while True:
            dynamic_pagination = await retries(lambda: user_obj.get_dynamics(offset=offset))
            if not dynamic_pagination.get('cards', []):
                break
                
            offset = dynamic_pagination.get('next_offset', 0)
            current_dynamics = dynamic_pagination['cards']
            filtered_dynamics = [
                d for d in current_dynamics
                if d['desc']['type'] != 8  # Filter out specific dynamic types
            ]
            dynamics_list.extend(filtered_dynamics)
            
            if len(dynamics_list) >= self.config.dynamic_count:
                break

        recent_dynamics = dynamics_list[:min(self.config.dynamic_count, len(dynamics_list))]

        def dynamic_oid(dynamic_: dict) -> int:
            """Get the object ID for a dynamic based on its type"""
            dynamic_type = dynamic_['desc']['type']
            if dynamic_type == 2:  # Type 2 is a special case
                return dynamic_['desc']['rid']
            else:
                return dynamic_['desc']['dynamic_id']

        self.new_dynamic_oids = [
            dynamic_oid(d) for d in dynamics_list 
            if d['desc'].get('timestamp', 0) > DISPLAY_BEFORE_TIMESTAMP
        ]

        async def get_comments(
                oid: int,
                type_: CommentResourceType,
                max_page: int,
                order: OrderType,
                ignore_list: set = None
        ) -> tuple:
            """Fetch comments for a given resource"""
            full_scrape = False
            if ignore_list is None:
                ignore_list = set()
                
            comments_dict = {}
            sub_comments_dict = {}
            
            for i in range(max_page):
                try:
                    # Get main comments
                    comments_result = await retries(
                        lambda: comment.get_comments(
                            oid=oid, 
                            type_=type_,
                            page_index=i + 1, 
                            order=order,
                            credential=self.config.credential
                        )
                    )
                except exceptions.ResponseCodeException as e:
                    print(f"错误代码{e.code}，停止抓取")
                    break
                    
                replies = comments_result.get('replies', []) or []
                
                # Process main comments
                for comment_data in replies:
                    if comment_data['rpid'] in ignore_list:
                        continue
                        
                    comments_dict[comment_data['rpid']] = comment_data
                    sub_comment_ids = []
                    scraped_sub_comments = False
                    
                    # Process sub-comments if any
                    if comment_data.get('replies'):
                        if comment_data.get('rcount', 0) > len(comment_data['replies']):
                            # Need to fetch more sub-comments
                            # Create a Comment object for the specific resource
                            from bilibili_api import comment as comment_module
                            comment_obj = comment_module.Comment(
                                oid=oid,
                                type_=type_,
                                rpid=comment_data['rpid'],
                                credential=self.config.credential
                            )
                            
                            page_index = 1
                            while True:
                                try:
                                    sub_comments_result = await retries(
                                        lambda: self.allow_blocked(
                                            lambda: comment_obj.get_sub_comments(page_index=page_index)
                                        )
                                    )
                                except Exception as e:
                                    print(f"获取子评论失败：{e}")
                                    print(traceback.format_exc())
                                    sub_comments_result = None
                                    
                                if sub_comments_result is None:
                                    sub_comment_ids = []
                                    break
                                    
                                subs = sub_comments_result.get('replies', []) or []
                                if not subs:
                                    scraped_sub_comments = True
                                    break
                                    
                                for sub in subs:
                                    comments_dict[sub['rpid']] = sub
                                    sub_comment_ids.append(sub['rpid'])
                                    
                                page_index += 1
                        else:
                            # All sub-comments are already included
                            for sub in comment_data['replies']:
                                comments_dict[sub['rpid']] = sub
                                sub_comment_ids.append(sub['rpid'])
                            scraped_sub_comments = True
                            
                    if scraped_sub_comments:
                        sub_comments_dict[comment_data['rpid']] = sub_comment_ids

                # If no more comments, we've done a full scrape
                if not replies:
                    full_scrape = True
                    break
                    
            return comments_dict, sub_comments_dict, full_scrape

        def update_comments(oname: str, oid: int, comments_time: dict, comments_like: dict, full_scrape=False):
            """Update comment records in the database"""
            if full_scrape:
                db_comments = [Comment(comment_, oname) for comment_ in comments_time.values()]
                db_comments += [Comment(comment_, oname) for comment_ in comments_like.values()]
                min_list = [comment_.ctime for comment_ in db_comments if comment_.root == 0]
                earliest_time = min(min_list) if min_list else None
            else:
                db_comments = [Comment(comment_, oname) for comment_ in comments_time.values()]
                min_list = [comment_.ctime for comment_ in db_comments if comment_.root == 0]
                earliest_time = min(min_list) if min_list else None
                db_comments += [Comment(comment_, oname) for comment_ in comments_like.values()]
                
            if earliest_time is not None:
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

            # Update sub-comments status
            for sub_comment_rpid, sub_comment_ids in sub_comments_dict.items():
                sub_comments = Comment.query.filter(
                    Comment.root == sub_comment_rpid
                ).all()
                for sub_comment in sub_comments:
                    if sub_comment.rpid not in sub_comment_ids:
                        sub_comment.guardian_status = -1
                    else:
                        sub_comment.guardian_status = 1

            # 确定新评论和重复评论
            filtered_db_comments = [comment_ for comment_ in db_comments if Comment.query.get(comment_.rpid) is None]
            duplicate_comments = len(db_comments) - len(filtered_db_comments)
            
            # 统计所有处理的评论数（新评论 + 重复评论）
            total_processed = len(db_comments)
            
            # 更新爬虫统计数据 - 记录处理的所有评论数
            if total_processed > 0:
                print(f"处理 {total_processed} 条评论（{len(filtered_db_comments)} 条新评论，{duplicate_comments} 条重复评论）")
                self.update_comment_rate(total_processed)  # 使用总处理数更新速率
            
            # 只保存新评论到数据库
            self.db.session.bulk_save_objects(filtered_db_comments)
            self.db.session.commit()

        # Process videos and get comments
        for video_data in tqdm.tqdm(recent_videos):
            # 更新爬虫统计数据 - 记录处理的视频数
            self.update_video_rate(1)
            
            # Track this video
            self.track_new_video()
            
            # Get comments sorted by time
            video_comments_time, video_sub_comments_time, full_scrape_time = await get_comments(
                video_data["aid"],
                type_=CommentResourceType.VIDEO,
                max_page=self.config.max_page,
                order=OrderType.TIME
            )
            
            # Get comments sorted by likes
            video_comments_likes, video_sub_comments_likes, full_scrape_like = await get_comments(
                video_data["aid"],
                type_=CommentResourceType.VIDEO,
                max_page=self.config.max_page,
                order=OrderType.LIKE,
                ignore_list=set(video_comments_time.keys())
            )
            
            all_rpid = set(video_comments_likes.keys()).union(video_comments_time.keys())
            sub_comments_dict = dict(video_sub_comments_time)
            sub_comments_dict.update(video_sub_comments_likes)

            update_comments(
                video_data['title'],
                video_data['aid'],
                video_comments_time,
                video_comments_likes,
                full_scrape_time and full_scrape_like
            )

        def dynamic_desc(dynamic_: dict) -> str:
            """Get a readable description of a dynamic"""
            dynamic_type = dynamic_['desc']['type']
            
            # Handle different dynamic types
            if dynamic_type == 1:  # Repost
                card_data = dynamic.get('card', {})
                if isinstance(card_data, str):
                    try:
                        import json
                        card_data = json.loads(card_data)
                    except:
                        card_data = {}
                        
                uname = "未知用户"
                if 'origin_user' in card_data:
                    if isinstance(card_data['origin_user'], dict) and 'info' in card_data['origin_user']:
                        uname = card_data['origin_user']['info'].get('uname', "未知用户")
                        
                content = card_data.get('item', {}).get('content', "")
                return f"{content}：转发\"{uname}\""
                
            elif dynamic_type == 2:  # Image dynamic
                card_data = dynamic.get('card', {})
                if isinstance(card_data, str):
                    try:
                        import json
                        card_data = json.loads(card_data)
                    except:
                        card_data = {}
                return card_data.get('item', {}).get('description', "")
                
            elif dynamic_type == 4:  # Text dynamic
                card_data = dynamic.get('card', {})
                if isinstance(card_data, str):
                    try:
                        import json
                        card_data = json.loads(card_data)
                    except:
                        card_data = {}
                return card_data.get('item', {}).get('content', "")
                
            return f"动态 {dynamic_['desc'].get('dynamic_id', '')}"

        def dynamic_resource_type(dynamic_: dict) -> CommentResourceType:
            """Determine the resource type of a dynamic for comment fetching"""
            dynamic_type = dynamic_['desc']['type']
            if dynamic_type == 2:
                return CommentResourceType.DYNAMIC_DRAW
            else:
                return CommentResourceType.DYNAMIC

        # Process dynamics and get comments
        for dynamic_data in tqdm.tqdm(recent_dynamics):
            # Get comments sorted by time
            dynamic_comments_time, dynamic_sub_comments_time, full_scrape_time = await get_comments(
                dynamic_oid(dynamic_data),
                type_=dynamic_resource_type(dynamic_data),
                max_page=self.config.max_page,
                order=OrderType.TIME
            )
            
            # Get comments sorted by likes
            dynamic_comments_likes, dynamic_sub_comments_likes, full_scrape_like = await get_comments(
                dynamic_oid(dynamic_data),
                type_=dynamic_resource_type(dynamic_data),
                max_page=self.config.max_page,
                order=OrderType.LIKE,
                ignore_list=set(dynamic_comments_time.keys())
            )
            
            all_rpid = set(dynamic_comments_likes.keys()).union(dynamic_comments_time.keys())
            sub_comments_dict = dict(dynamic_sub_comments_time)
            sub_comments_dict.update(dynamic_sub_comments_likes)

            update_comments(
                dynamic_desc(dynamic_data),
                dynamic_oid(dynamic_data),
                dynamic_comments_time,
                dynamic_comments_likes,
                full_scrape_like and full_scrape_time
            )
        
        # 打印当前速率统计
        print(f"当前爬虫速率: {self.scraper_stats['comment_rate']}条评论/秒, {self.scraper_stats['video_rate']}个视频/分")
        print(f"最近30分钟记录: {len(self.comment_records)}条评论批次, {len(self.video_records)}条视频批次")
        
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
        import threading
        import asyncio
        
        # Create a new event loop for the background thread
        background_loop = asyncio.new_event_loop()
        
        # Define thread function
        def thread_target():
            asyncio.set_event_loop(background_loop)
            try:
                background_loop.run_until_complete(self.scraper_loop())
            except Exception as e:
                print(f"Scraper failed: {e}")
                import traceback
                print(traceback.format_exc())
        
        # Start background thread
        scraper_thread = threading.Thread(target=thread_target, daemon=True)
        scraper_thread.start()
        
        print("Scraper background thread started")

    # 新增: 更新评论记录并计算速率
    def update_comment_rate(self, count):
        """记录一批评论并更新评论处理速率"""
        now = datetime.now()
        
        # 添加新记录
        if count > 0:
            self.comment_records.append((count, now))
            print(f"记录 {count} 条新评论，时间: {now}")
        
        # 清除超过30分钟的记录
        cutoff = now - timedelta(minutes=30)
        self.comment_records = [r for r in self.comment_records if r[1] >= cutoff]
        
        # 计算最近30分钟的评论总数和最早时间点
        total_comments = sum(record[0] for record in self.comment_records)
        if len(self.comment_records) > 1:  # 至少要有初始记录和一条新记录
            earliest = self.comment_records[0][1]
            duration = (now - earliest).total_seconds()
            if duration > 0:
                self.scraper_stats['comment_rate'] = round(total_comments / duration, 1)
    
    # 新增: 更新视频记录并计算速率
    def update_video_rate(self, count):
        """记录一批视频并更新视频处理速率"""
        now = datetime.now()
        
        # 添加新记录
        if count > 0:
            self.video_records.append((count, now))
            print(f"记录 {count} 个新视频，时间: {now}")
        
        # 清除超过30分钟的记录
        cutoff = now - timedelta(minutes=30)
        self.video_records = [r for r in self.video_records if r[1] >= cutoff]
        
        # 计算最近30分钟的视频总数和最早时间点
        total_videos = sum(record[0] for record in self.video_records)
        if len(self.video_records) > 1:  # 至少要有初始记录和一条新记录
            earliest = self.video_records[0][1]
            duration = (now - earliest).total_seconds()
            if duration > 0:
                self.scraper_stats['video_rate'] = round(total_videos * 60 / duration, 1)  # 转换为每分钟

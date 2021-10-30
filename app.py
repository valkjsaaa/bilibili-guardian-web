import argparse
import os
from datetime import datetime

from bilibili_api.comment import ResourceType
from flask import Flask, render_template, request
from flask_cors import CORS, cross_origin
from flask_script import Manager

from config import Config
from dataset import db, Comment
from scraper import Scraper

app = Flask(__name__)
cors = CORS(app)
config: Config


@cross_origin()
@app.route('/comments', methods=['GET'])
def comments():  # put application's code here
    type_ = request.args.get('type')
    page = request.args.get('pn')
    if type_ != "dynamic":
        type_ = "video"
    if page is None:
        page = 1
    else:
        page = int(page)
    per_page = 50
    if type_ == "dynamic":
        page_comments = Comment.query. \
            filter(Comment.guardian_status != -1). \
            filter(Comment.type_.in_([ResourceType.DYNAMIC.value, ResourceType.DYNAMIC_DRAW.value])). \
            order_by(Comment.ctime.desc()).paginate(page, per_page, error_out=False)
    else:
        page_comments = Comment.query. \
            filter(Comment.guardian_status != -1). \
            filter_by(type_=ResourceType.VIDEO.value). \
            order_by(Comment.ctime.desc()).paginate(page, per_page, error_out=False)
    return render_template(
        'comments.html',
        comments=page_comments,
        type_=type_,
        last_refreshed=datetime.now() - scraper.last_refreshed if scraper.last_refreshed is not None else None
    )



manager = Manager(app)


@manager.command
def runserver():
    app.run()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Start Bilibili guardian server")
    parser.add_argument('--db', type=str, help="path to database file", required=True)
    parser.add_argument('--user', type=int, help="user id")
    parser.add_argument('--video_count', type=int, help="video count")
    parser.add_argument('--dynamic_count', type=int, help="dynamic count")
    parser.add_argument('--max_page', type=int, help="maximum pages to scrap")

    args = parser.parse_args()
    app.config['SQLALCHEMY_DATABASE_URI'] = args.db

    app.app_context().push()

    db.init_app(app)
    db.create_all()

    config_dict = {}
    if args.user is not None:
        config_dict['user'] = args.user
    if args.video_count is not None:
        config_dict['video_count'] = args.video_count
    if args.dynamic_count is not None:
        config_dict['dynamic_count'] = args.dynamic_count
    if args.max_page is not None:
        config_dict['max_page'] = args.max_page
    if 'URL' in os.environ:
        app.config['SERVER_NAME'] = os.environ['URL']

    config = Config(**config_dict)
    scraper = Scraper(config, db, app)
    scraper.run_scraper()

    app.config['PREFERRED_URL_SCHEME'] = 'https'
    app.run(host='0.0.0.0', port=5000)

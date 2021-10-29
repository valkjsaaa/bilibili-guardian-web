import argparse

from flask import Flask, render_template
from flask_script import Manager

from config import Config
from dataset import db, Comment
from scraper import Scraper

app = Flask(__name__)
config: Config


@app.route('/comments/<int:page>', methods=['GET'])
def comments(page=1):  # put application's code here
    per_page = 50
    page_comments = Comment.query.order_by(Comment.ctime.desc()).paginate(page, per_page, error_out=False)
    return render_template('comments.html', comments=page_comments)


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

    config = Config(**config_dict)
    scraper = Scraper(config, db, app)
    scraper.run_scraper()

    app.run(host='0.0.0.0')

import argparse
import os
import ssl
from datetime import datetime, timedelta

from bilibili_api.comment import CommentResourceType
from flask import Flask, render_template, request, Response
from flask_cors import CORS, cross_origin

from config import Config
from dataset import db, Comment
from scraper import Scraper

app = Flask(__name__)
cors = CORS(app)
config: Config


def get_statistics():
    """Get statistics for the dashboard"""
    stats = {
        'last_refreshed': datetime.now() - scraper.last_refreshed if scraper.last_refreshed else None,
        'total_comments': Comment.query.count(),
        'video_comments': Comment.query.filter_by(type_=CommentResourceType.VIDEO.value).count(),
        'dynamic_comments': Comment.query.filter(Comment.type_.in_([
            CommentResourceType.DYNAMIC.value, CommentResourceType.DYNAMIC_DRAW.value
        ])).count(),
        'flagged_comments': Comment.query.filter(Comment.guardian_status == 2).count(),
        'deleted_comments': Comment.query.filter(Comment.guardian_status == -1).count(),
    }
    
    # 爬虫的评论处理速率
    if hasattr(scraper, 'scraper_stats'):
        stats['comments_per_second'] = scraper.scraper_stats.get('comment_rate', 0)
        stats['videos_per_minute'] = scraper.scraper_stats.get('video_rate', 0)
        
        # 添加最近30分钟的处理统计
        if hasattr(scraper, 'comment_records'):
            total_recent_comments = sum(record[0] for record in scraper.comment_records)
            stats['recent_comments'] = total_recent_comments
        else:
            stats['recent_comments'] = 0
            
        if hasattr(scraper, 'video_records'):
            total_recent_videos = sum(record[0] for record in scraper.video_records)
            stats['recent_videos'] = total_recent_videos
        else:
            stats['recent_videos'] = 0
    else:
        stats['comments_per_second'] = 0
        stats['videos_per_minute'] = 0
        stats['recent_comments'] = 0
        stats['recent_videos'] = 0
    
    # Calculate unique content stats
    stats['unique_videos'] = db.session.query(Comment.oid).filter_by(type_=CommentResourceType.VIDEO.value).distinct().count()
    stats['unique_dynamics'] = db.session.query(Comment.oid).filter(Comment.type_.in_([
        CommentResourceType.DYNAMIC.value, CommentResourceType.DYNAMIC_DRAW.value
    ])).distinct().count()
    
    # Get unique commenters
    stats['unique_users'] = db.session.query(Comment.mid).distinct().count()
    
    return stats


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
            filter(Comment.type_.in_([CommentResourceType.DYNAMIC.value, CommentResourceType.DYNAMIC_DRAW.value])). \
            order_by(Comment.ctime.desc()).paginate(page, per_page, error_out=False)
    else:
        page_comments = Comment.query. \
            filter(Comment.guardian_status != -1). \
            filter_by(type_=CommentResourceType.VIDEO.value). \
            order_by(Comment.ctime.desc()).paginate(page, per_page, error_out=False)
    
    stats = get_statistics()
    
    return render_template(
        'comments.html',
        comments=page_comments,
        type_=type_,
        last_refreshed=stats['last_refreshed'],
        stats=stats
    )


@cross_origin()
@app.route('/try_delete_comment', methods=['POST'])
def try_delete_comment():
    rpid = request.form.get('rpid')
    comment = Comment.query.get(rpid)
    if comment is None:
        return Response('{"message":"未能找到对应评论"}', status=404, mimetype='application/json')
    else:
        if comment.guardian_status not in [0, 1]:
            return Response('{"message":"评论已被删除或正在被删除"}', status=304, mimetype='application/json')
        else:
            comment.guardian_status = 2
            db.session.commit()
            return Response('{"message":"已经记录"}', status=202, mimetype='application/json')


@cross_origin()
@app.route('/bad_users', methods=['GET'])
def bad_users():  # put application's code here
    all_deleted_comments = Comment.query.filter(Comment.guardian_status == -1).all()
    user_count_list = {}
    for comment in all_deleted_comments:
        if comment.mid in user_count_list:
            user_count_list[comment.mid]["count"] += 1
            user_count_list[comment.mid]["last"] = comment
        else:
            user_count_list[comment.mid] = {
                "count": 1,
                "last": comment
            }

    users = [{
        "uid": user_count["last"].mid,
        "uname": user_count["last"].mname,
        "last": user_count["last"].create_time_utc8(),
        "count": user_count["count"],
        "top_bad": False
    } for user_count in user_count_list.values()]

    users.sort(key=lambda user: user["count"], reverse=True)

    for user_id, user_obj in enumerate(users[:9]):
        user_comments = Comment.query. \
            filter(~Comment.oid.in_(scraper.new_video_oids + scraper.new_dynamic_oids)). \
            filter(Comment.mid == user_obj["uid"]). \
            filter(Comment.guardian_status.in_([0, 1])). \
            all()
        user_comments_json = \
            [{"type": comment.type_, "oid": str(comment.oid), "rpid": str(comment.rpid)} for comment in user_comments]
        users[user_id]['comments'] = user_comments_json
        users[user_id]['top_bad'] = True
    
    stats = get_statistics()
    
    return render_template(
        'bad_users.html',
        users=users,
        type_="bad_users",
        last_refreshed=stats['last_refreshed'],
        stats=stats
    )


def generate_ssl_context(cert_dir="ssl"):
    """Generate a self-signed SSL certificate if it doesn't exist"""
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    import datetime
    
    # Create directory if it doesn't exist
    if not os.path.exists(cert_dir):
        os.makedirs(cert_dir)
    
    cert_file = os.path.join(cert_dir, "cert.pem")
    key_file = os.path.join(cert_dir, "key.pem")
    
    # Check if files already exist
    if os.path.exists(cert_file) and os.path.exists(key_file):
        print(f"Using existing SSL certificate: {cert_file} and key: {key_file}")
        return (cert_file, key_file)
    
    # Generate a new key and certificate
    print("Generating self-signed SSL certificate...")
    
    # Generate private key
    key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    
    # Write private key
    with open(key_file, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        ))
    
    # Generate a certificate
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, u"CN"),
        x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, u"Beijing"),
        x509.NameAttribute(NameOID.LOCALITY_NAME, u"Beijing"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, u"Bilibili Guardian"),
        x509.NameAttribute(NameOID.COMMON_NAME, u"localhost"),
    ])
    
    cert = x509.CertificateBuilder().subject_name(
        subject
    ).issuer_name(
        issuer
    ).public_key(
        key.public_key()
    ).serial_number(
        x509.random_serial_number()
    ).not_valid_before(
        datetime.datetime.utcnow()
    ).not_valid_after(
        # Our certificate will be valid for 1 year
        datetime.datetime.utcnow() + datetime.timedelta(days=365)
    ).add_extension(
        x509.SubjectAlternativeName([x509.DNSName(u"localhost")]),
        critical=False,
    ).sign(key, hashes.SHA256())
    
    # Write certificate
    with open(cert_file, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    
    print(f"SSL certificate generated: {cert_file} and key: {key_file}")
    return (cert_file, key_file)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Start Bilibili guardian server")
    parser.add_argument('--db', type=str, help="path to database file", required=True)
    parser.add_argument('--user', type=int, help="user id")
    parser.add_argument('--video_count', type=int, help="video count")
    parser.add_argument('--dynamic_count', type=int, help="dynamic count")
    parser.add_argument('--max_page', type=int, help="maximum pages to scrap")
    parser.add_argument('--username', type=str, help="username")
    parser.add_argument('--password', type=str, help="password")
    parser.add_argument('--sessdata', type=str, help="sessdata")
    parser.add_argument('--bili_jct', type=str, help="bili_jct")
    parser.add_argument('--buvid3', type=str, help="buvid3 cookie")
    parser.add_argument('--https', action='store_true', help="enable HTTPS with self-signed certificate")
    parser.add_argument('--port', type=int, default=5000, help="port to run server on")

    args = parser.parse_args()
    app.jinja_env.auto_reload = True
    app.config['TEMPLATES_AUTO_RELOAD'] = True
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
    if args.username is not None:
        config_dict['username'] = args.username
    if args.password is not None:
        config_dict['password'] = args.password
    if args.sessdata is not None:
        config_dict['sessdata'] = args.sessdata
    if args.bili_jct is not None:
        config_dict['bili_jct'] = args.bili_jct
    if args.buvid3 is not None:
        config_dict['buvid3'] = args.buvid3
    if 'URL' in os.environ:
        app.config['SERVER_NAME'] = os.environ['URL']

    config = Config(**config_dict)
    scraper = Scraper(config, db, app)
    
    # Use asyncio to run Flask properly with the new scraper setup
    import asyncio
    from werkzeug.serving import run_simple
    
    # Set up scraper in event loop
    scraper.run_scraper()
    
    # Run Flask with the existing event loop
    ssl_context = None
    if args.https:
        try:
            # Try to generate or use existing SSL certificate
            ssl_context = generate_ssl_context()
            print(f"HTTPS enabled on port {args.port}")
        except Exception as e:
            print(f"Error setting up HTTPS: {e}")
            print("Falling back to HTTP")
    
    run_simple('0.0.0.0', args.port, app, use_reloader=False, threaded=True, ssl_context=ssl_context)

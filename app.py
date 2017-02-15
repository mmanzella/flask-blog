import datetime
import functools
import os
import re
import urllib
import smtplib

from flask import (Flask, flash, Markup, redirect, render_template, request,
                   Response, session, url_for)
from markdown import markdown
from markdown.extensions.codehilite import CodeHiliteExtension
from markdown.extensions.extra import ExtraExtension
from micawber import bootstrap_basic, parse_html
from micawber.cache import Cache as OEmbedCache
from peewee import *
from playhouse.flask_utils import FlaskDB, get_object_or_404, object_list
from playhouse.sqlite_ext import *


# Blog configuration values.

# You may consider using a one-way hash to generate the password, and then
# use the hash again in the login view to perform the comparison. This is just
# for simplicity.
APP_DIR = os.path.dirname(os.path.realpath(__file__))

# The playhouse.flask_utils.FlaskDB object accepts database URL configuration.
DATABASE = SqliteExtDatabase('blog_app.db')
DEBUG = False

SECRET_KEY = '9mw^qR$$'
USERNAME = 'root'
PASSWORD = 'root'

# This is used by micawber, which will attempt to generate rich media
# embedded objects with maxwidth=800.
SITE_WIDTH = 800


# Create a Flask WSGI app and configure it using values from the module.
app = Flask(__name__)
app.config.from_object(__name__)

# FlaskDB is a wrapper for a peewee database that sets up pre/post-request
# hooks for managing database connections.
flask_db = FlaskDB(app)

# The `database` is the actual peewee database, as opposed to flask_db which is
# the wrapper.
database = flask_db.database

# Configure micawber with the default OEmbed providers (YouTube, Flickr, etc).
# We'll use a simple in-memory cache so that multiple requests for the same
# video don't require multiple network requests.
oembed_providers = bootstrap_basic(OEmbedCache())

# CLI helper function
def create_tables():
    database.connect()
    database.create_tables([Tag, BlogEntryTag])

########################################################################


class BaseModel(flask_db.Model):
    class Meta:
        database = database

class Entry(BaseModel):
    title = CharField()
    slug = CharField(unique=True)
    content = TextField()
    published = BooleanField(index=True)
    timestamp = DateTimeField(default=datetime.datetime.now, index=True)

    @property
    def html_content(self):
        """
        Generate HTML representation of the markdown-formatted blog entry,
        and also convert any media URLs into rich media objects such as video
        players or images.
        """
        hilite = CodeHiliteExtension(linenums=False, css_class='highlight')
        extras = ExtraExtension()
        markdown_content = markdown(self.content, extensions=[hilite, extras])
        oembed_content = parse_html(
            markdown_content,
            oembed_providers,
            urlize_all=True,
            maxwidth=app.config['SITE_WIDTH'])
        return Markup(oembed_content)

    def save(self, *args, **kwargs):
        # Generate a URL-friendly representation of the entry's title.
        if not self.slug:
            self.slug = re.sub('[^\w]+', '-', self.title.lower()).strip('-')
        ret = super(Entry, self).save(*args, **kwargs)

        import pdb
        # pdb.set_trace()
        for tag in self.tags:
            tag = re.sub('[^\w]+', '-', tag.lower()).strip('-')
            BlogEntryTag.create(entry_id=self.id, name=tag)

        #self.html_content(self)

        # Store search content.
        self.update_search_index()
        self.update_tags()
        return ret

    def update_search_index(self):
        # Create a row in the FTSEntry table with the post content. This will
        # allow us to use SQLite's awesome full-text search extension to
        # search our entries.
        query = (FTSEntry
                 .select(FTSEntry.docid, FTSEntry.entry_id)
                 .where(FTSEntry.entry_id == self.id))
        try:
            fts_entry = query.get()
        except FTSEntry.DoesNotExist:
            fts_entry = FTSEntry(entry_id=self.id)
            force_insert = True
        else:
            force_insert = False
        tags_str = ','.join(self.tags)
        fts_entry.content = '\n'.join((self.title, self.content, tags_str))
        fts_entry.save(force_insert=force_insert)

    def update_tags(self):
        import pdb
        #pdb.set_trace()

        to_update = Tag.update(count=Tag.count+1).where(Tag.tag<<self.tags)
        to_update.execute()
        """
        try:
            update_fts = FTSEntry.create(FTSEntry.tags==self.tags, FTSEntry.entry_id==self.id)
        except IntegrityError:
            update_fts = FTSEntry.update(FTSEntry.tags==self.tags).where(FTSEntry.entry_id==self.id)
        update_fts.execute()"""
        existing_tags = [x.tag for x in Tag.select(Tag.tag).where(Tag.tag<<self.tags)]
        for tag in self.tags:
            tag = re.sub('[^\w]+', '-', tag.lower()).strip('-')
            if tag not in existing_tags:
                Tag.create(tag=tag, count=1)
            else:
                pass

    @classmethod
    def public(cls):
        return Entry.select().where(Entry.published == True)

    @classmethod
    def drafts(cls):
        return Entry.select().where(Entry.published == False)

    @classmethod
    def search(cls, query):
        words = [word.strip() for word in query.split() if word.strip()]
        if not words:
            # Return an empty query.
            return Entry.select().where(Entry.id == 0)
        else:
            search = ' '.join(words)

        # Query the full-text search index for entries matching the given
        # search query, then join the actual Entry data on the matching
        # search result.
        import pdb
        #pdb.set_trace()
        return (FTSEntry
                .select(
                    FTSEntry,
                    Entry,
                    FTSEntry.rank().alias('score'))
                .join(Entry, on=(FTSEntry.entry_id == Entry.id).alias('entry'))
                .where(
                    (Entry.published == True) &
                    (FTSEntry.match(search)))
                .order_by(SQL('score').desc()))

# Full Text Search
class FTSEntry(FTSModel):
    entry_id = IntegerField(Entry)
    content = TextField()
    tags = TextField()

    class Meta:
        database = database

class BlogEntryTag(BaseModel):
    entry = ForeignKeyField(Entry, related_name='tags')
    name = CharField()

    class Meta:
        database = database

class Tag(BaseModel):
    tag = CharField(unique=True)
    count = IntegerField()

    class Meta:
        database = database



##############################################################



def login_required(fn):
    @functools.wraps(fn)
    def inner(*args, **kwargs):
        if session.get('logged_in'):
            return fn(*args, **kwargs)
        return redirect(url_for('login', next=request.path))
    return inner

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if (request.form['username'] != app.config['USERNAME']) or (request.form['password'] != app.config['PASSWORD']):
            error = flash('Invalid username or password', 'danger')
        else:
            session['logged_in'] = True
            flash('You were logged in successfully', 'success')
            return redirect(url_for('index'))
    return render_template('login.html', error=error)

@app.route('/logout', methods=['GET', 'POST'])
def logout():
    if request.method == 'POST':
        session.clear()
        message = flash('You were logged out successfully', 'success')
        return redirect(url_for('login'))
    return render_template('logout.html')

@app.route('/')
def index():
    search_query = request.args.get('q')
    if search_query:
        query = Entry.search(search_query)
    else:
        query = Entry.public().order_by(Entry.timestamp.desc())

    # The `object_list` helper will take a base query and then handle
    # paginating the results if there are more than 20. For more info see
    # the docs:
    # http://docs.peewee-orm.com/en/latest/peewee/playhouse.html#object_list
    return object_list(
        'index.html',
        query,
        search=search_query,
        check_bounds=False)

@app.route('/about')
def about():
    return render_template('about.html')

# Method not a "view"
def _create_or_edit(entry, template):
    if request.method == 'POST':
        entry.title = request.form.get('title') or ''
        entry.content = request.form.get('content') or ''
        entry.published = request.form.get('published') or False
        import pdb
        #pdb.set_trace()
        entry.tags = request.form.get('tags').strip().split(', ') or False
        if not (entry.title and entry.content):
            flash('Title and Content are required.', 'danger')
        else:
            # Wrap the call to save in a transaction so we can roll it back
            # cleanly in the event of an integrity error.
            try:
                with database.atomic():
                    entry.save()
            except IntegrityError:
                flash('Error: this title is already in use.', 'danger')
            else:
                flash('Entry saved successfully.', 'success')
                if entry.published:
                    return redirect(url_for('detail', slug=entry.slug))
                else:
                    return redirect(url_for('edit', slug=entry.slug))


    return render_template(template, entry=entry)

@app.route('/create/', methods=['GET', 'POST'])
@login_required
def create():
    return _create_or_edit(Entry(title='', content='', tags=''), 'create.html')

@app.route('/drafts/')
@login_required
def drafts():
    query = Entry.drafts().order_by(Entry.timestamp.desc())
    return object_list('index.html', query, check_bounds=False)

@app.route('/<slug>/')
def detail(slug):
    if session.get('logged_in'):
        query = Entry.select()
        import pdb
        #pdb.set_trace()
    else:
        query = Entry.public()
    entry = get_object_or_404(query, Entry.slug == slug)
    return render_template('detail.html', entry=entry)

@app.route('/<slug>/edit/', methods=['GET', 'POST'])
@login_required
def edit(slug):
    entry = get_object_or_404(Entry, Entry.slug == slug)
    return _create_or_edit(entry, 'edit.html')

@app.template_filter('list_all_tags')
def list_all_tags(request_args):
    tag_list = Tag.select().order_by(Tag.count.desc())
    return tag_list

@app.route('/tags/')
def tags():
    query = Tag.select().order_by(Tag.tag.desc())
    import pdb
    #pdb.set_trace()
    return object_list('tags.html', query, check_bounds=False)

@app.route('/tags/<tag_name>/')
def blogs_by_tag(tag_name):
    import pdb
    #pdb.set_trace()
    blog_entry_list = [x.entry.id for x in BlogEntryTag.select().where(BlogEntryTag.name==tag_name) if x]
    entry_by_tag_list = Entry.select().where(Entry.id<<blog_entry_list)
    return object_list('index.html', entry_by_tag_list, check_bounds=False)

@app.route('/send_email/', methods=['POST'])
def send_email():
    import pdb
    pdb.set_trace()

@app.template_filter('clean_querystring')
def clean_querystring(request_args, *keys_to_remove, **new_values):
    # We'll use this template filter in the pagination include. This filter
    # will take the current URL and allow us to preserve the arguments in the
    # querystring while replacing any that we need to overwrite. For instance
    # if your URL is /?q=search+query&page=2 and we want to preserve the search
    # term but make a link to page 3, this filter will allow us to do that.
    querystring = dict((key, value) for key, value in request_args.items())
    for key in keys_to_remove:
        querystring.pop(key, None)
    querystring.update(new_values)
    return urllib.urlencode(querystring)

@app.template_filter('make_markdown')
def make_markdown(markdown_content):
    return Markup(markdown(markdown_content))

@app.errorhandler(404)
def not_found(exc):
    return Response('<h3>Not found</h3>'), 404

def main():
    database.create_tables([Entry, FTSEntry], safe=True)
    app.run(debug=True)

if __name__ == '__main__':
    main()

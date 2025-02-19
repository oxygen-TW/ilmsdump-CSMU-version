from __future__ import annotations

import asyncio
import collections
import contextlib
import dataclasses
import functools
import getpass
import hashlib
import itertools
import json
import os
import pathlib
import pickle
import re
import shlex
import shutil
import signal
import sys
import time
import types
from typing import AsyncGenerator, Awaitable, Iterable, Iterator, List, Optional, Union

import aiohttp
import click
import lxml.html
import wcwidth
import yarl

import ilmsdump.fileutil

LOGIN_DOMAIN = 'lms.csmu.edu.tw'
TARGET_ORIGIN = os.environ.get('ILMSDUMP_TARGET_ORIGIN', 'http://lms.csmu.edu.tw')
LOGIN_URL = f'{TARGET_ORIGIN}/sys/lib/ajax/login_submit.php'
LOGIN_STATE_URL = f'{TARGET_ORIGIN}/home.php'
COURSE_LIST_URL = f'{TARGET_ORIGIN}/home.php?f=allcourse'


class ILMSError(Exception):
    """Base exception class for ilmsdump"""


class LoginFailed(ILMSError):
    """Failed to login"""


class CannotUnderstand(ILMSError):
    """Server returned something unexpected"""


class UserError(ILMSError):
    """Invalid user input"""


class Unavailable(ILMSError):
    """Requested resource does not exist"""

    @classmethod
    def check(cls, html: lxml.html.HtmlElement):
        errors = html.xpath('//body/div[count(./*)=0]/text()')
        if errors:
            raise cls(errors[0])


class DownloadFailed(ILMSError):
    """Failed to perform download"""


class NoPermission(ILMSError):
    """
    No Permission! Read permission : Only open for teacher and TA
    權限不足! 目前課程的閱讀權限為 : 不開放(僅老師及助教可以閱讀)
    """

    @classmethod
    def check(cls, html: lxml.html.HtmlElement):
        no_permission = html.xpath(
            '//div[contains(@style, "color:#F00;") and '
            '(starts-with(text(), "權限不足!") or starts-with(text(), "No Permission!"))]'
            '/text()'
        )
        if no_permission:
            raise cls(*no_permission)


def as_sync(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))

    return wrapper


def as_sync_cooperative(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(func(*args, **kwargs))

    return wrapper


class _EmptyAsyncGenerator:
    def __init__(self, coro: Awaitable):
        self._done = False
        self._coro = coro

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._done:
            self._done = True
            await self._coro
        raise StopAsyncIteration


def as_empty_async_generator(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return _EmptyAsyncGenerator(func(*args, **kwargs))

    return wrapper


@contextlib.contextmanager
def capture_keyboard_interrupt() -> Iterator[asyncio.Event]:
    event = asyncio.Event()

    def handler(signum, frame):
        event.set()

    old_handler = signal.signal(signal.SIGINT, handler)
    try:
        yield event
    finally:
        signal.signal(signal.SIGINT, old_handler)


@as_sync_cooperative
async def _get_workaround_client_response_content_is_traced():
    is_traced = False

    async def callback(session, context, params):
        nonlocal is_traced
        is_traced = True

    tc = aiohttp.TraceConfig()
    tc.on_response_chunk_received.append(callback)

    async with aiohttp.ClientSession(trace_configs=[tc]) as client:
        async with client.get(f'{TARGET_ORIGIN}') as response:
            async for chunk in response.content.iter_any():
                pass
    return is_traced


# https://github.com/aio-libs/aiohttp/issues/5324
_workaround_client_response_content_is_traced = _get_workaround_client_response_content_is_traced()


def qs_get(url: str, key: str) -> str:
    purl = yarl.URL(url)
    try:
        return purl.query[key]
    except KeyError:
        raise KeyError(key, url) from None


@functools.singledispatch
def quote_path(path):
    raise NotImplementedError


@quote_path.register
def _(path: pathlib.PurePosixPath):
    return shlex.quote(str(path))


@quote_path.register
def _(path: pathlib.PureWindowsPath):
    pathstr = str(path)
    if '"' in pathstr:
        raise ValueError(f'Invalid path containing double quotes: {path!r}')
    return f'"{pathstr}"'


class Client:
    def __init__(self, data_dir):
        self.bytes_downloaded = 0

        trace_config = aiohttp.TraceConfig()
        trace_config.on_response_chunk_received.append(self.session_on_response_chunk_received)

        self.session = aiohttp.ClientSession(
            raise_for_status=True,
            trace_configs=[trace_config],
            timeout=aiohttp.ClientTimeout(total=80),
        )

        self.data_dir = pathlib.Path(data_dir).absolute()
        os.makedirs(self.data_dir, exist_ok=True)

        self.cred_path = os.path.join(self.data_dir, 'credentials.txt')

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def close(self):
        await self.session.close()

    log = staticmethod(print)

    @contextlib.asynccontextmanager
    async def request(self, *args, **kwargs):
        retries = 3
        sleep_duration = 5
        while True:
            try:
                async with self.session.request(*args, **kwargs) as response:
                    yield response
                    return
            except aiohttp.ClientResponseError as exc:
                if not retries:
                    raise
                if exc.status != 400:
                    raise
                print(file=sys.stderr)
                print(f'Exception occurred: {exc}', file=sys.stderr)
                print(
                    f'Sleeping for {sleep_duration}s; remaining retries: {retries}',
                    file=sys.stderr,
                )
                await asyncio.sleep(sleep_duration)
                sleep_duration *= 4
                retries -= 1

    async def ensure_authenticated(self, prompt: bool):
        try:
            cred_file = open(self.cred_path, encoding='utf-8')
        except FileNotFoundError:
            if prompt:
                await self.interactive_login()
                with open(self.cred_path, 'w', encoding='utf-8') as file:
                    print(
                        self.session.cookie_jar.filter_cookies(yarl.URL(LOGIN_STATE_URL))[
                            'PHPSESSID'
                        ].value,
                        file=file,
                    )
                self.log('Saved credentials to', self.cred_path)
        else:
            with cred_file:
                self.log('Using existing credentials in', self.cred_path)
                phpsessid = cred_file.read().strip()
                await self.login_with_phpsessid(phpsessid)

    def clear_credentials(self):
        """
        Clear saved credentials. Returns true if something is actually removed. False otherwise.
        """
        try:
            os.remove(self.cred_path)
        except FileNotFoundError:
            self.log('No credentials saved in', self.cred_path)
            return False
        else:
            self.log('Removed saved credentials in', self.cred_path)
            return True

    async def interactive_login(self):
        username = input('iLMS username (leave empty to login with PHPSESSID): ')
        if username:
            password = getpass.getpass('iLMS password: ')
            await self.login_with_username_and_password(username, password)
        else:
            phpsessid = getpass.getpass('iLMS PHPSESSID: ')
            await self.login_with_phpsessid(phpsessid)

    async def login_with_username_and_password(self, username, password):
        from ilmsdump import captcha

        async with self.session.get(f'{TARGET_ORIGIN}/login_page.php') as response:
            await response.read()

        # async with captcha.request(self.session) as response:
        #     jpegbin = await response.read()
        captcha_code = "na"

        login = self.session.post(
            LOGIN_URL,
            data={
                'account': username,
                'password': password,
                'secCode': captcha_code,
            },
        )
        async with login as response:
            response.raise_for_status()
            json_body = await response.json(
                content_type=None,  # to bypass application/json check
            )
        json_ret = json_body['ret']
        if json_ret['status'] != 'true':
            raise LoginFailed(json_ret)
        self.log('Logged in as', json_ret['name'])

    async def login_with_phpsessid(self, phpsessid):
        self.session.cookie_jar.update_cookies(
            {'PHPSESSID': phpsessid},
            response_url=yarl.URL(LOGIN_DOMAIN),
        )
        name = await self.get_login_state()
        if name is None:
            raise LoginFailed('cannot login with provided PHPSESSID')
        self.log('Logged in as', name)

    async def get_login_state(self):
        async with self.session.get(LOGIN_STATE_URL) as response:
            html = lxml.html.fromstring(await response.text())

            if not html.xpath('//*[@id="login"]'):
                return None

            name_node = html.xpath('//*[@id="profile"]/div[2]/div[1]/text()')
            assert name_node
            return ''.join(name_node).strip()

    async def session_on_response_chunk_received(
        self,
        session: aiohttp.ClientSession,
        context: types.SimpleNamespace,
        params: aiohttp.TraceResponseChunkReceivedParams,
    ) -> None:
        self.bytes_downloaded += len(params.chunk)

    async def get_course(self, course_id: int) -> 'Course':
        async with self.session.get(
            f'{TARGET_ORIGIN}/course.php',
            params={
                'courseID': course_id,
                'f': 'syllabus',
            },
        ) as response:
            print(response.url)
            if response.url.path == '/course_login.php':
                raise UserError(f'No access to course: course_id={course_id}')

            body = await response.text()
            if not body:
                raise UserError(
                    'Empty response returned for course, '
                    f"the course probably doesn't exist: course_id={course_id}"
                )

            html = lxml.html.fromstring(body)
            print("1")
            (name,) = html.xpath('//span[@class="pointer"]/text()')

            # (hint,) = html.xpath('//div[@class="infoTable"]//td[2]/span[@class="hint"]/text()')
            # m = re.match(r'\(\w+, (\w+), \w+, \w+\)', hint)
            # assert m is not None, hint
            # serial = m.group(1)

            if html.xpath('//div[@id="main"]//a[@href="javascript:editDoc(1)"]'):
                is_admin = True
            else:
                is_admin = False

            course = Course(
                id=course_id,
                serial="--",
                name=name,
                is_admin=is_admin,
            )

            return course

    async def get_enrolled_courses(self) -> AsyncGenerator['Course', None]:
        async with self.session.get(COURSE_LIST_URL) as response:
            body = await response.text()
            html = lxml.html.fromstring(body)

            try:
                Unavailable.check(html)
            except Unavailable:
                raise UserError('Cannot get enrolled courses. Are you logged in?')

            for a in html.xpath('.//td[@class="listTD"]/a'):
                bs = a.xpath('b')
                if bs:
                    is_admin = True
                    (tag,) = bs
                else:
                    is_admin = False
                    tag = a

                name = tag.text
                serial = a.getparent().getparent()[0].text

                m = re.match(r'/course/(\d+)', a.attrib['href'])
                if m is None:
                    raise CannotUnderstand('course URL', a.attrib['href'])
                yield Course(
                    id=int(m.group(1)),
                    serial=serial,
                    name=name,
                    is_admin=is_admin,
                )

    async def get_open_courses(self, semester_id=-1) -> AsyncGenerator['Course', None]:
        page = 1
        total_pages = 1
        while page <= total_pages:
            print(end=f'\rIndexing open courses: page {page} of {total_pages}', file=sys.stderr)
            async with self.session.get(
                f'{TARGET_ORIGIN}/course/index.php',
                params={
                    'nav': 'course',
                    't': 'open',
                    'term': semester_id,
                    'page': page,
                },
            ) as response:
                html = lxml.html.fromstring(await response.text())

            total_pages_strs = html.xpath('//input[@id="PageCombo"]/following-sibling::text()')
            if total_pages_strs:
                total_pages = int(total_pages_strs[0].rpartition('/')[2])
            else:
                for href in html.xpath('//span[@class="page"]/span[@class="item"]/a/@href'):
                    total_pages = max(total_pages, int(qs_get(href, 'page')))

            for a in html.xpath('//div[@class="tableBox"]//a[starts-with(@href, "/course/")]'):
                id_ = int(os.path.basename(a.attrib['href']))
                title = a.text
                serial_div = a.getparent().getprevious()[0]
                assert serial_div.tag == 'div'
                assert serial_div.attrib['title'] == serial_div.text
                serial = serial_div.text
                yield Course(
                    id=id_,
                    serial=serial,
                    name=title,
                    is_admin=False,
                )

            page += 1
        print()

    def get_dir_for(self, item: Downloadable) -> pathlib.Path:
        d = self.data_dir / item.__class__.__name__.lower() / str(item.id)
        d.mkdir(parents=True, exist_ok=True)
        return d


class Downloadable:

    _CLASSES: List[str] = []
    id: int

    @as_empty_async_generator
    async def download(self, client):
        pass

    def as_id_string(self):
        return f'{self.__class__.__name__}-{self.id}'

    def get_meta(self) -> dict:
        return {
            field.name: flatten_attribute(getattr(self, field.name))
            for field in dataclasses.fields(self)
        }

    @classmethod
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        Downloadable._CLASSES.append(cls.__name__)


@functools.singledispatch
def flatten_attribute(value):
    return value


@flatten_attribute.register
def _(value: Downloadable):
    return value.as_id_string()


@flatten_attribute.register
def _(value: yarl.URL):
    return str(value)


@dataclasses.dataclass
class Stat:
    total: int = 0
    completed: int = 0


class Downloader:
    def __init__(self, client: Client):
        self.client = client
        self.stats = collections.defaultdict(Stat)
        self.fullstats = collections.Counter()
        self.rates = collections.deque(maxlen=20)
        self.rates_str = '  0.00Mbps'
        self.done: Optional[asyncio.Event] = None
        self.report_progress_task: Optional[asyncio.Task] = None

    def mark_total(self, item):
        self.stats[item.STATS_NAME].total += 1

    def mark_completed(self, item):
        self.stats[item.STATS_NAME].completed += 1
        self.fullstats[item.__class__.__name__] += 1

    def report_progress(self):
        progress_str = '  '.join(f'{k}:{v.completed}/{v.total}' for (k, v) in self.stats.items())
        dl_size_str = f'{self.client.bytes_downloaded / 1e6:.1f}MB'
        print(
            f'{self.rates_str}  DL:{dl_size_str}  {progress_str}'.ljust(
                max(1, shutil.get_terminal_size().columns - 1)
            ),
            end='\r',
            file=sys.stderr,
        )

    def update_rates(self):
        now = time.perf_counter()
        bandwidth = 0
        if self.rates:
            then, old_size = self.rates[0]
            bandwidth = (self.client.bytes_downloaded - old_size) / (now - then)
        self.rates.append((now, self.client.bytes_downloaded))
        self.rates_str = f'{bandwidth*8e-6:6.2f}Mbps'

    async def periodically_report_progress(self, done: asyncio.Event, period: float = 0.5):
        while not done.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(done.wait(), period)
            self.update_rates()
            self.report_progress()

    def create_resume_file(self, data):
        b = pickle.dumps(data)
        sha256 = hashlib.sha256(b).hexdigest()
        resume_file = self.client.data_dir / f'resume-{sha256[:7]}.pickle'
        resume_file.write_bytes(b)
        return resume_file

    async def run(self, items: Iterable[Downloadable], ignore=()):
        print('--- Starting Download '.ljust(79, '-'))

        items = collections.deque(items)

        for item in items:
            self.mark_total(item)

        self.done = asyncio.Event()
        self.report_progress_task = asyncio.create_task(
            self.periodically_report_progress(self.done),
        )

        with capture_keyboard_interrupt() as interrupted:

            while items:
                item = items[0]

                if item.__class__.__name__ in ignore or item.as_id_string() in ignore:
                    items.popleft()
                    continue

                item_children = []

                try:
                    async for child in item.download(self.client):
                        item_children.append(child)
                        self.mark_total(child)

                    with (self.client.get_dir_for(item) / 'meta.json').open(
                        'w', encoding='utf-8'
                    ) as file:
                        json.dump(
                            {
                                **item.get_meta(),
                                'children': [c.as_id_string() for c in item_children],
                            },
                            file,
                        )
                except Exception:
                    resume_file = self.create_resume_file(dict(items=items, ignore=ignore))
                    await self.finish()
                    raise DownloadFailed(
                        f'Error occurred while handling {item}\n'
                        f'Run with --resume={quote_path(resume_file)} to resume download.\n'
                        f'Run with --ignore={item.as_id_string()} to ignore this item.'
                    )

                items.popleft()
                items.extend(item_children)
                self.mark_completed(item)

                if interrupted.is_set():
                    print(file=sys.stderr)
                    resume_file = self.create_resume_file(dict(items=items, ignore=ignore))
                    await self.finish()
                    print(
                        'Interrupted.\n'
                        f'Run with --resume={quote_path(resume_file)} to resume download.\n'
                        f'Run with --ignore={item.as_id_string()} to ignore this item.',
                        file=sys.stderr,
                    )
                    return

        await self.finish()

    async def finish(self):
        self.done.set()
        assert self.report_progress_task is not None
        await self.report_progress_task

        self.report_progress()
        print(file=sys.stderr)
        print('--- Summary '.ljust(79, '-'))
        for k, v in self.fullstats.items():
            print(f'{k}: {v}')
        print('-' * 79)


def html_get_main(html: lxml.html.HtmlElement) -> lxml.html.HtmlElement:
    NoPermission.check(html)
    mains = html.xpath('//div[@id="main"]')
    if not mains:
        raise Unavailable(
            '//div[@id="main"] not found: {}'.format(
                ''.join(map(str.strip, html.xpath('//text()')))[:100]
            )
        )
    main = mains[0]
    for to_remove in itertools.chain(
        main.xpath('div[@class="infoPath"]'),
        main.xpath('.//script'),
    ):
        to_remove.getparent().remove(to_remove)
    return main


def table_is_empty(html: lxml.html.HtmlElement) -> bool:
    second_row_tds = html.xpath('//div[@class="tableBox"]/table/tr[2]/td')
    if len(second_row_tds) == 1:
        # 目前尚無資料 or No Data
        assert second_row_tds[0].text in ('目前尚無資料', 'No Data')
        return True
    return False


def get_attachments(parent: Downloadable, element: lxml.html.HtmlElement) -> Iterator['Attachment']:
    ids = set()
    for a in element.xpath('.//a[starts-with(@href, "/sys/read_attach.php")]'):
        if a.text is None or not a.text.strip():
            continue
        url = yarl.URL(a.attrib['href'])
        id_ = int(url.query['id'])
        if id_ in ids:
            continue
        ids.add(id_)
        title = a.attrib.get('title', a.text)
        yield Attachment(
            id=id_,
            title=title,
            parent=parent,
        )


@dataclasses.dataclass
class Course(Downloadable):
    """歷年課程檔案"""

    id: int
    serial: str  # 科號
    is_admin: bool
    name: str

    STATS_NAME = 'Course'

    async def download(self, client):
        generators = [
            self.get_announcements(client),
            self.get_materials(client),
            self.get_discussions(client),
            self.get_homeworks(client),
            self.get_scores(client),
            self.get_grouplists(client),
        ]
        for generator in generators:
            async for item in generator:
                yield item

        async with client.request(
            'GET',
            f'{TARGET_ORIGIN}/course.php',
            params={
                'courseID': self.id,
                'f': 'syllabus',
            },
        ) as response:
            html = lxml.html.fromstring(await response.text())

        main = html_get_main(html)
        with (client.get_dir_for(self) / 'index.html').open('wb') as file:
            file.write(lxml.html.tostring(main))

    async def _item_paginator(self, client, f, page=1):
        for page in itertools.count(page):
            async with client.request(
                'GET',
                f'{TARGET_ORIGIN}/course.php',
                params={
                    'courseID': self.id,
                    'f': f,
                    'page': page,
                },
            ) as response:
                html = lxml.html.fromstring(await response.text())

                if table_is_empty(html):
                    break

                yield html

                next_hrefs = html.xpath('//span[@class="page"]//a[text()="Next"]/@href')
                if not next_hrefs:
                    break
                next_page = int(qs_get(next_hrefs[0], 'page'))
                assert page + 1 == next_page

    async def get_announcements(self, client) -> AsyncGenerator['Announcement', None]:
        async for html in self._item_paginator(client, 'news'):
            for tr in html.xpath('//*[@id="main"]//tr[@class!="header"]'):
                (href,) = tr.xpath('td[1]/a/@href')
                (title,) = tr.xpath('td[2]//a/text()')
                yield Announcement(
                    id=int(qs_get(href, 'newsID')),
                    title=title,
                    course=self,
                )

    async def get_materials(self, client) -> AsyncGenerator['Material', None]:
        async for html in self._item_paginator(client, 'doclist'):
            for a in html.xpath('//*[@id="main"]//tr[@class!="header"]/td[2]/div/a'):
                url = yarl.URL(a.attrib['href'])
                if url.path != '/course.php' or url.query['f'] != 'doc':
                    # linked material (the copy should still be downloaded)
                    # XXX: this cannot be tested without logging in :(
                    continue
                yield Material(
                    id=int(url.query['cid']),
                    title=a.text,
                    type=a.getparent().attrib['class'],
                    course=self,
                )

    async def get_discussions(self, client) -> AsyncGenerator['Discussion', None]:
        async for html in self._item_paginator(client, 'forumlist'):
            for tr in html.xpath('//*[@id="main"]//tr[@class!="header"]'):
                if tr.xpath('.//img[@class="vmiddle"]'):
                    # XXX: belongs to a homework, material
                    # don't know if it is accessible
                    continue
                (href,) = tr.xpath('td[1]/a/@href')
                (title,) = tr.xpath('td[2]//a/span/text()')
                yield Discussion(
                    id=int(qs_get(href, 'tid')),
                    title=title,
                    course=self,
                )

    async def get_homeworks(self, client) -> AsyncGenerator['Homework', None]:
        async for html in self._item_paginator(client, 'hwlist'):
            for a in html.xpath('//*[@id="main"]//tr[@class!="header"]/td[2]/a[1]'):
                yield Homework(
                    id=int(qs_get(a.attrib['href'], 'hw')),
                    title=a.text,
                    course=self,
                )

    async def get_scores(self, client) -> AsyncGenerator['Score', None]:
        async with client.request(
            'GET',
            f'{TARGET_ORIGIN}/course.php',
            params={
                'f': 'score',
                'courseID': self.id,
            },
        ) as response:
            html = lxml.html.fromstring(await response.text())
            if not html.xpath(
                '//div[@id="main"]//input[@type="button" and @onclick="history.back()"]'
            ):
                yield Score(course=self)

    async def get_grouplists(self, client) -> AsyncGenerator['GroupList', None]:
        async with client.request(
            'GET',
            f'{TARGET_ORIGIN}/course.php',
            params={
                'f': 'grouplist',
                'courseID': self.id,
            },
        ) as response:
            html = lxml.html.fromstring(await response.text())
            if not table_is_empty(html):
                yield GroupList(course=self)


@dataclasses.dataclass
class Announcement(Downloadable):
    """課程活動(公告)"""

    id: int
    title: str
    course: Course

    STATS_NAME = 'Page'

    async def download(self, client: Client):
        async with client.request(
            'GET',
            f'{TARGET_ORIGIN}/home/http_event_select.php',
            params={
                'id': self.id,
                'type': 'n',
            },
        ) as response:
            body_json = await response.json(content_type=None)

        if body_json['news']['note'] == 'NA' and body_json['news']['poster'] == '':
            raise Unavailable(body_json)

        attachment_raw_div = body_json['news']['attach']
        if attachment_raw_div is not None:
            for attachment in get_attachments(self, lxml.html.fromstring(attachment_raw_div)):
                yield attachment

        with (client.get_dir_for(self) / 'index.json').open('w', encoding='utf-8') as file:
            json.dump(body_json, file)


@dataclasses.dataclass
class Material(Downloadable):
    """上課教材"""

    id: int
    title: str
    type: str  # "Econtent" or "Epowercam"
    course: Course

    STATS_NAME = 'Page'

    async def download(self, client: Client):
        async with client.request(
            'GET',
            f'{TARGET_ORIGIN}/course.php',
            params={
                'courseID': self.course.id,
                'f': 'doc',
                'cid': self.id,
            },
        ) as response:
            html = lxml.html.fromstring(await response.text())
        main = html_get_main(html)

        for attachment in get_attachments(self, main):
            yield attachment

        if self.type == 'Epowercam':
            video = await self.get_video(client, response.url)
            if video is not None:
                yield video

        with (client.get_dir_for(self) / 'index.html').open('wb') as file:
            file.write(lxml.html.tostring(main))

    async def get_video(self, client: Client, base_url: yarl.URL) -> Union[None, 'Video']:
        async with client.request(
            'GET',
            f'{TARGET_ORIGIN}/sys/http_get_media.php',
            params={
                'id': self.id,
                'db_table': 'content',
                'flash_installed': 'false',
                'swf_id': f'swfslide{self.id}',
                'area_size': '724x3',
            },
        ) as response:
            body_json = await response.json(content_type=None)
        if body_json['ret']['status'] != 'true':
            raise CannotUnderstand(f'Video not found: {self}, {body_json}')
        if body_json['ret']['player_width'] is None:
            # 轉檔中
            # {"ret":{"status":"true","id":"2475544","embed":"...",
            # "player_width":null,"player_height":null}}
            return None
        html = lxml.html.fromstring(body_json['ret']['embed'])
        (src,) = html.xpath('//video/@src')
        return Video(id=self.id, url=base_url.join(yarl.URL(src)))


@dataclasses.dataclass
class Discussion(Downloadable):
    """討論區"""

    id: int
    title: str
    course: Course

    STATS_NAME = 'Page'

    async def download(self, client: Client):
        async with client.request(
            'GET',
            f'{TARGET_ORIGIN}/sys/lib/ajax/post.php',
            params={
                'id': self.id,
            },
        ) as response:
            body_json = await response.json(content_type=None)
            if body_json['posts']['status'] != 'true':
                raise CannotUnderstand(body_json)

            for post in body_json['posts']['items']:
                for attachment in post['attach']:
                    yield Attachment(
                        id=int(attachment['id']),
                        title=attachment['srcName'],
                        parent=self,
                    )

            with (client.get_dir_for(self) / 'index.json').open('w', encoding='utf-8') as file:
                json.dump(body_json, file)


@dataclasses.dataclass
class Homework(Downloadable):
    """作業"""

    id: int
    title: str
    course: Course

    STATS_NAME = 'Page'

    async def download(self, client: Client):
        # homework description
        async with client.request(
            'GET',
            f'{TARGET_ORIGIN}/course.php',
            params={
                'courseID': self.course.id,
                'f': 'hw',
                'hw': self.id,
            },
        ) as response:
            html = lxml.html.fromstring(await response.text())
        main = html_get_main(html)
        for to_remove in main.xpath('.//span[@class="toolWrapper"]'):
            to_remove.getparent().remove(to_remove)

        for attachment in get_attachments(self, main):
            yield attachment

        with (client.get_dir_for(self) / 'index.html').open('wb') as file:
            file.write(lxml.html.tostring(main))

        # submitted homework
        async with client.request(
            'GET',
            f'{TARGET_ORIGIN}/course.php',
            params={
                'courseID': self.course.id,
                'f': 'hw_doclist',
                'hw': self.id,
            },
        ) as response:
            html = lxml.html.fromstring(await response.text())

        main = html_get_main(html)

        if table_is_empty(main):
            return

        (header_tr,) = main.xpath('.//div[@class="tableBox"]//tr[@class="header"]')
        field_indexes = {}
        for i, td in enumerate(header_tr):
            try:
                (a,) = td.xpath('a')
            except ValueError:
                continue
            field_indexes[qs_get(a.attrib['href'], 'order')] = i

        ititle = field_indexes['title']
        assert ititle == 1
        iname = field_indexes['name']
        assert iname > ititle

        for tr in main.xpath('//div[@class="tableBox"]//tr[@class!="header"]'):
            a_s = tr[ititle].xpath('div/a')
            if not a_s:
                continue
            (a,) = a_s
            id_ = int(qs_get(a.attrib['href'], 'cid'))
            title = a.text

            comments = tr[ititle].xpath('div/img[@src="/sys/res/icon/hw_comment.png"]/@title')
            if comments:
                (comment,) = comments
            else:
                comment = None

            # Group homework may hide behind a <a>
            (by,) = tr[iname].xpath('div/text()|div/a/text()')

            yield SubmittedHomework(
                id=id_,
                title=title,
                by=by,
                comment=comment,
                course=self.course,
            )

        with (client.get_dir_for(self) / 'list.html').open('wb') as file:
            file.write(lxml.html.tostring(main))


@dataclasses.dataclass
class SubmittedHomework(Downloadable):
    """
    作業 -> 已交名單 -> 標題[點進去]
    """

    id: int
    title: str
    by: str
    course: Course
    comment: Optional[str] = None

    STATS_NAME = 'Page'

    async def download(self, client: Client):
        async with client.request(
            'GET',
            f'{TARGET_ORIGIN}/course.php',
            params={
                'courseID': self.course.id,
                'f': 'doc',
                'cid': self.id,
            },
        ) as response:
            html = lxml.html.fromstring(await response.text())

        main = html_get_main(html)

        for attachment in get_attachments(self, main):
            yield attachment

        with (client.get_dir_for(self) / 'index.html').open('wb') as file:
            file.write(lxml.html.tostring(main))


@dataclasses.dataclass
class SinglePageDownloadable(Downloadable):
    course: Course

    STATS_NAME = 'Page'

    @property
    def extra_params(self) -> dict:
        raise NotImplementedError

    @property
    def id(self):
        return self.course.id

    @as_empty_async_generator
    async def download(self, client: Client):
        async with client.request(
            'GET',
            f'{TARGET_ORIGIN}/course.php',
            params={
                'courseID': self.course.id,
                **self.extra_params,
            },
        ) as response:
            html = lxml.html.fromstring(await response.text())
            main = html_get_main(html)

            with (client.get_dir_for(self) / 'index.html').open('wb') as file:
                file.write(lxml.html.tostring(main))


class Score(SinglePageDownloadable):
    """
    成績計算
    """

    extra_params = {'f': 'score'}


class GroupList(SinglePageDownloadable):
    """
    小組專區
    """

    extra_params = {'f': 'grouplist'}


@dataclasses.dataclass
class Attachment(Downloadable):
    id: int
    title: str
    parent: Downloadable

    STATS_NAME = 'File'

    @as_empty_async_generator
    async def download(self, client):
        async with client.request(
            'GET',
            f'{TARGET_ORIGIN}/sys/read_attach.php',
            params={
                'id': self.id,
            },
        ) as response:
            with (client.get_dir_for(self) / self.suggest_filename()).open('wb') as file:
                async for chunk in response.content.iter_any():
                    if not _workaround_client_response_content_is_traced:
                        client.bytes_downloaded += len(chunk)
                    file.write(chunk)

    def get_meta(self) -> dict:
        return {
            **super().get_meta(),
            'saved_filename': self.suggest_filename(),
        }

    def suggest_filename(self) -> str:
        name = os.path.basename(self.title)
        if name == 'meta.json':
            return 'meta_.json'
        return ilmsdump.fileutil.replace_illegal_characters_in_path(name, '_')


@dataclasses.dataclass
class Video(Downloadable):
    id: int
    url: yarl.URL

    STATS_NAME = 'File'

    @as_empty_async_generator
    async def download(self, client):
        async with client.request('GET', self.url) as response:
            with (client.get_dir_for(self) / 'video.mp4').open('wb') as file:
                async for chunk in response.content.iter_any():
                    if not _workaround_client_response_content_is_traced:
                        client.bytes_downloaded += len(chunk)
                    file.write(chunk)


def generate_table(items):
    fields = [field.name for field in dataclasses.fields(items[0])]
    rows = [fields]
    rows.extend([str(getattr(item, field)) for field in fields] for item in items)
    widths = [max(map(wcwidth.wcswidth, col)) for col in zip(*rows)]
    for i, row in enumerate(rows):
        for j, (width, cell) in enumerate(zip(widths, row)):
            if j:
                yield '  '
            yield cell
            if j + 1 < len(row):
                yield ' ' * (width - wcwidth.wcswidth(cell))
        yield '\n'
        if i == 0:
            for j, width in enumerate(widths):
                if j:
                    yield '  '
                yield '-' * width
            yield '\n'


def print_table(items):
    print(end=''.join(generate_table(items)))


async def foreach_course(
    client: Client, course_ids: List[Union[str, int]]
) -> AsyncGenerator[Course, None]:
    for course_id in course_ids:
        if course_id == 'enrolled':
            async for course in client.get_enrolled_courses():
                yield course
        elif course_id == 'open':
            async for course in client.get_open_courses():
                yield course
        else:
            yield await client.get_course(int(course_id))


def validate_course_id(ctx, param, value: str):
    result: List[Union[str, int]] = []
    for course_id in value:
        if course_id in {'enrolled', 'open'}:
            result.append(course_id)
        elif not course_id.isdigit():
            raise click.BadParameter('must be a number or the string "enrolled"')
        else:
            result.append(int(course_id))
    return result


class CLISystemExit(SystemExit):
    pass


@click.command(
    help="""
        Dump the courses given by their ID.

        The string "enrolled" can be used as a special ID to dump all courses
        enrolled by the logged in user.

        The string "open" can be used as a special ID to dump all open courses.
        Downloading all open courses generates a lot of load & traffic on the server.
        Proceed with caution.
        """,
)
@click.option(
    '--logout',
    is_flag=True,
    help="""
        Clear iLMS credentials.
        If specified with --login, the credentials is cleared first, then login is performed
    """,
)
@click.option(
    '--login',
    is_flag=True,
    help='Login to iLMS interactively before accessing iLMS',
)
@click.option(
    '--anonymous',
    is_flag=True,
    help='Ignore stored credentials',
)
@click.option(
    '-o',
    '--output-dir',
    metavar='DIR',
    default='ilmsdump.out',
    show_default=True,
    help='Output directory to store login credentials and downloads',
)
@click.option(
    '--ignore',
    multiple=True,
    help=f'''Ignore items specied as `CLASS` or `CLASS-ID`.

    Valid CLASSes are: {', '.join(Downloadable._CLASSES)}.

    Example: --ignore=Course-74 ignores Course with ID 74.
    --ignore=Video ignores all videos.''',
)
@click.option(
    '--dry',
    is_flag=True,
    help='List matched courses only. Do not download',
)
@click.option(
    '--resume',
    metavar='FILE',
    help='Resume download',
)
@click.option(
    '--no-resume-check',
    is_flag=True,
    help='Allow --resume and COURSE_IDS specified at the same time',
)
@click.argument(
    'course_ids',
    nargs=-1,
    callback=validate_course_id,
)
@as_sync
async def main(
    course_ids,
    logout: bool,
    login: bool,
    anonymous: bool,
    output_dir: str,
    dry: bool,
    resume: str,
    ignore: list,
    no_resume_check: bool,
):
    if not no_resume_check:
        if resume is not None and course_ids:
            raise CLISystemExit(
                '''\
Error. Under usual cases, you do not need to specify COURSE_IDS when resuming.
Specifying --resume and COURSE_IDS at the same time may download a resource multiple times.
You can add --no-resume-check to bypass this check if you are sure what you are doing.'''
            )

    async with Client(data_dir=output_dir) as client:
        d = Downloader(client=client)
        changed = False
        if logout:
            changed |= client.clear_credentials()

        if not anonymous:
            await client.ensure_authenticated(prompt=login)
            changed |= login

        targets = []
        ignores = set(ignore)
        if resume is not None:
            with open(resume, 'rb') as file:
                resmue_data = pickle.load(file)
                targets.extend(resmue_data['items'])
                ignores.update(resmue_data['ignore'])
        if course_ids:
            courses = [course async for course in foreach_course(client, course_ids)]
            if courses:
                print(end=''.join(generate_table(courses)))
            targets.extend(courses)

        if targets:
            changed = True
            if not dry:
                await d.run(targets, ignore=set(ignore))
        if not changed:
            click.echo('Nothing to do', err=True)


if __name__ == '__main__':
    main()

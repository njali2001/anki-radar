# anki-radar

A small read-only script that finds forum posts where someone is stuck on Anki
syncing or collection size, and prints them as a list of links for a human to
read and answer.

**It never posts anything.** No comments, no replies, no votes, no messages, no
account actions of any kind. It produces a local HTML page of links; the replies
are written and posted by a person, from their own account. There is no code path
in this repository that writes to any forum — see [What it does not do](#what-it-does-not-do).

I run a paid Anki sync hosting service, so when I answer someone and mention it,
I say so in the reply. That disclosure is a rule of this project, not an
afterthought: undisclosed promotion is both against community norms and, in the
US, against the FTC endorsement guidelines.

## What it does

1. Reads public sources for a small set of keywords:
   - the official Anki forum (`forums.ankiweb.net`, Discourse search JSON)
   - Reddit, both as a site-wide search (`reddit.com/search.rss`) and as the
     new-post feeds of a few named subreddits. The search matters more than the
     list: someone stuck on a sync is not necessarily posting in r/Anki, and a
     week's hits routinely span half a dozen subreddits nobody would have thought
     to subscribe to. What a search returns is not filtered against the keyword
     list afterwards — the query is the filter, and re-filtering would drop
     exactly the posts worth reading, such as a bare "Help, cannot boot up Anki".
   - YouTube comments (official Data API v3, needs a free key), for the
     Portuguese- and English-speaking study crowds: the same pattern as
     Bilibili. A Brazilian who cannot get a sync to finish does not open a
     forum thread; they say so under "Como sincronizar o Anki com o celular",
     which has tens of thousands of views. One pass returned seven real
     complaints where a month of Portuguese Reddit searching returned none.
   - Bilibili (`bilibili.com`), for the Chinese-speaking side: it searches for
     videos, then reads the **comments** under the few whose title or
     description look relevant. The videos themselves are almost always
     tutorials, often a year or two old; the person who cannot get a sync to
     finish is in the comment section underneath, and that comment is recent.
     Videos are therefore discovery only and are not stored unless you set
     `include_videos`. An anonymous reader is served the three newest comments
     under a video and no more, so the tool covers more videos rather than
     paging deeper, and gives Bilibili a longer age window than the other two —
     Anki is a niche topic there and comments arrive months apart.
2. Keeps keyword matches from the last N days in a local SQLite file. Matching is
   whole-word for ASCII keywords; a keyword containing non-ASCII characters is
   matched as a substring, because written Chinese has no spaces between words
   and a word boundary would never hold.
3. Writes `report.html` — one card per hit with a direct link to the thread.
4. Anything that has appeared in a report never appears in another one, so each
   run shows only what is new.

Typical volume: a handful of posts per day. This is a reading list, not a feed.

## What it does not do

- It does not post, comment, vote, message, or follow anyone.
- It does not log in. Reddit access is anonymous (public RSS); the Anki forum is
  read through its public search endpoint; Bilibili's search and comment
  endpoints are read anonymously and need no key.
- It does not store user profiles, build audience datasets, or track individuals.
  Each row is a link, a title, a short excerpt, and which keyword matched.
- It does not run continuously. A run is a handful of HTTP requests, with a
  mandatory pause between them (configurable, default 20s for Reddit), and is
  meant to be run a few times a day at most.
- It does not keep knocking after a source pushes back. A rate-limited pass
  stops there instead of moving on to the next subreddit or keyword, keeps
  whatever it already fetched, and puts that source on a cooldown (15 minutes by
  default, or whatever `Retry-After` asked for) during which the button is grey.
- It has no scheduler, queue, or worker. It is one script you run by hand.

## Running it

    run.bat                      open the local page          (Windows)
    python radar.py --serve      same thing                   (any OS)

The page runs on 127.0.0.1. The left column lists the sources — each with how
many items are waiting, when it was last scanned, whether it can be scanned right
now, and its own scan button — and the right column shows the selected source's
list. Clicking a source switches the list; it never starts a scan — a Reddit pass takes minutes, and wanting to read
yesterday's leftovers should not cost that. Scanning is the button inside the
tab. The Anki forum is scanned on startup (seconds); Reddit and Bilibili wait for
a click, because both need a long pause between requests. The page remembers
which tab you were on, so the reload after a scan leaves you where you were.

Each tab states when that source was last scanned, to the minute. Reddit and
Bilibili also have a minimum interval between scans (`min_interval_minutes`):
until it has passed, the scan button is greyed out and the server refuses the
request anyway, because a greyed-out button is a hint and the page can be
reloaded. Scanning either of them again within the hour spends requests to fetch
the same posts back. The forum has no interval — a pass there takes seconds. Each post has **写要点 / 已处理 / 忽略**
(notes / done / ignore) buttons: what you act on disappears, what you don't is
still there next time.

**写要点** asks the model for material to answer with — the facts that apply,
what to check first, what not to claim — and not for a finished reply. The reply
is written by a person, in their own words. Generating it costs a request, so it
happens when you click, not for every card.

**回复** opens a box to write that reply in Chinese and puts it into whatever
language the original comment used — the model reads the original to decide,
so there is no language to pick. This is translation, not ghostwriting: the
words and the judgement stay the author's, and product names, error messages and
menu labels are kept verbatim so they can be quoted back. Foreign-language items
are also translated into Chinese under the original when they are scored, so the
whole loop — read, decide, answer — works in a language the author reads.

One-off runs without the page:

    python radar.py --forum-only --no-open   scan the forum, write report.html
    python radar.py --sample                 offline sample data, no network
                                             (kept in its own database file)
    python radar.py --again                  reopen the last report
    python radar.py --stats                  which keyword produced how many hits

Starting it a second time while it is already running does not start a second
copy: it says so and exits. (On Windows two processes can otherwise bind the same
port, and requests then land on either one — which looks like code changes
randomly not taking effect.)

No dependencies — Python standard library and SQLite only.

## Configuration

Copy `config.example.json` to `config.json` and edit. Both sources work without
any credentials.

    user_agent      identify yourself; Reddit asks for this and rate-limits
                    vague ones. ASCII only (HTTP headers cannot hold anything else).
    keywords        whole-word, case-insensitive. Pick phrases only Anki users
                    would write; there is no AND across separate words. They
                    filter the feeds, not the searches. A trailing `*` matches a
                    stem: Portuguese conjugates one verb into sincronizar,
                    sincroniza, sincronizando and sincronização, and whole-word
                    matching catches none of them.
    searches        Reddit queries, each with a short label that becomes the
                    reason the item was kept. Boolean syntax works:
                    anki (sync OR ankiweb OR syncing).
    max_age_days    older threads have usually been answered already.
    pause_seconds   delay between HTTP requests. Raise it if you see HTTP 429.
    daily_limit     how many items land in one report.
    youtube         off until you add an API key. In the Google Cloud console,
                    enable YouTube Data API v3 and create an API key — the
                    Gemini key from AI Studio is a different kind and returns
                    401 here. A search costs 100 quota units and reading one
                    video's comments costs 1, against 10,000 free per day.
    ai              optional. With an API key and enabled=true, keyword hits are
                    scored 0-3 for "is this person actually stuck on syncing or
                    size"; only 2+ is shown, with a one-line reason. Without a
                    key the tool falls back to keywords alone. A second provider
                    can be listed as a fallback for when the first one is busy.
                    A source may set its own min_score: a thin source can afford
                    a lower bar than a busy one, where a weak hit only crowds out
                    a real one. `--reconsider <source>` brings back items that an
                    earlier, stricter bar had dropped.
    bilibili        search terms (which videos to look at) and keywords (which
                    of them are worth reading comments under, and which comments
                    to keep). max_videos_for_comments caps the requests: one per
                    video.

## Why not the Reddit API

Since Reddit's Responsible Builder Policy (November 2025), OAuth app registration
is no longer self-service: access is granted per request through a support ticket.
This script therefore reads the public RSS feeds instead, at a deliberately low
rate. If API access is granted, swapping the source is a small change — but the
"never posts" property above would stay exactly as it is.

## Files

    radar.py            entry point: scan, filter, score, render
    sources.py          Anki forum + Reddit RSS + Bilibili readers (stdlib only)
    ai.py               optional relevance scoring (Gemini / OpenAI-compatible)
    ui.py               the local page: one button per source, no external access
    store.py            SQLite storage, de-duplication, your done/ignore marks
    sample_posts.json   offline sample data for --sample
    config.example.json copy to config.json (which is gitignored)

Code comments are in Chinese — this started as a personal tool. The README,
the CLI help and the configuration file are in English.

## License

MIT — see [LICENSE](LICENSE).

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
   - public subreddit feeds (`reddit.com/r/<sub>/new.rss`)
2. Keeps whole-word keyword matches from the last N days in a local SQLite file.
3. Writes `report.html` — one card per hit with a direct link to the thread.
4. Anything that has appeared in a report never appears in another one, so each
   run shows only what is new.

Typical volume: a handful of posts per day. This is a reading list, not a feed.

## What it does not do

- It does not post, comment, vote, message, or follow anyone.
- It does not log in. Reddit access is anonymous (public RSS); the Anki forum is
  read through its public search endpoint.
- It does not store user profiles, build audience datasets, or track individuals.
  Each row is a link, a title, a short excerpt, and which keyword matched.
- It does not run continuously. A run is a handful of HTTP requests, with a
  mandatory pause between them (configurable, default 20s for Reddit), and is
  meant to be run a few times a day at most.
- It has no scheduler, queue, or worker. It is one script you run by hand.

## Running it

    run.bat                 scan and open the report      (Windows)
    python radar.py         same thing                    (any OS)
    python radar.py --sample     offline sample data, no network
    python radar.py --forum-only only the Anki forum, no Reddit
    python radar.py --again      reopen the last report
    python radar.py --stats      which keyword produced how many hits

No dependencies — Python standard library and SQLite only.

## Configuration

Copy `config.example.json` to `config.json` and edit. Both sources work without
any credentials.

    user_agent      identify yourself; Reddit asks for this and rate-limits
                    vague ones. ASCII only (HTTP headers cannot hold anything else).
    keywords        whole-word, case-insensitive. Pick phrases only Anki users
                    would write; there is no AND across separate words.
    max_age_days    older threads have usually been answered already.
    pause_seconds   delay between HTTP requests. Raise it if you see HTTP 429.
    daily_limit     how many items land in one report.

## Why not the Reddit API

Since Reddit's Responsible Builder Policy (November 2025), OAuth app registration
is no longer self-service: access is granted per request through a support ticket.
This script therefore reads the public RSS feeds instead, at a deliberately low
rate. If API access is granted, swapping the source is a small change — but the
"never posts" property above would stay exactly as it is.

## Files

    radar.py            entry point: scan, filter, report
    sources.py          Anki forum + Reddit RSS readers (stdlib only)
    store.py            SQLite storage and de-duplication
    sample_posts.json   offline sample data for --sample
    config.example.json copy to config.json (which is gitignored)

Code comments are in Chinese — this started as a personal tool. The README,
the CLI help and the configuration file are in English.

## License

MIT — see [LICENSE](LICENSE).

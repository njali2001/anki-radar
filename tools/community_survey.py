# -*- coding: utf-8 -*-
"""Anki 的人都待在 Reddit 的哪些版块——用真实返回统计，不靠印象。

    runtime\\python.exe tools\\community_survey.py            # 最近一个月
    runtime\\python.exe tools\\community_survey.py --window year --pause 20

【和雷达的日常扫描是两件事】：日常扫描找的是"正卡着、值得回复的人"，用的是很
窄的关键词；这个脚本问的是另一个问题——**Anki 的重度用户平时在哪儿说话**，所以
查询词故意放宽（就是 anki 本身），要的是版块的分布，不是具体哪条帖子。

结论拿来干什么：决定 config.json 里 `subreddits` 那张订阅表该放谁。订阅表里的
版块会被逐个拉 new.rss，**多一个版块就是多一个请求**，而 Reddit 按请求数限流，
所以那张表应该短而准——这个脚本就是用来判断"准"的。

【只读】：和这个仓库里的其他东西一样，它只读公开 RSS，什么都不发。
"""

import argparse
import collections
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import sources  # noqa: E402
from paths import CONFIG, EXAMPLE  # noqa: E402

# 【查询词要宽】：日常扫描那几条是 "anki (sync OR ankiweb…)"，那是找求助的人。
# 这里要的是"谁在用 Anki"，所以只问 anki 本身，再补几条能捞到不同人群的说法。
QUERIES = [
    "anki",
    "anki deck",
    "anki cards",
    "ankidroid",
    "anki reviews",
    "flashcards anki",
]


# 【定向验证用的候选人群】：宽查询只能告诉你"这个月谁在说话"，样本薄的时候
# "没出现"既可能是真没有，也可能只是没抽到。要判断某个人群到底用不用 Anki，
# 得按版块直接问——subreddit: 是 Reddit 搜索自带的算子。
TARGETED = {
    "法律": "anki (subreddit:LawSchool OR subreddit:barexam OR subreddit:LSAT)",
    "护理": "anki (subreddit:StudentNurse OR subreddit:NCLEX OR subreddit:nursing)",
    "药学牙科": "anki (subreddit:PharmacySchool OR subreddit:Dentistry OR subreddit:predental)",
    "金融考证": "anki (subreddit:CFA OR subreddit:actuary OR subreddit:Accounting)",
    "语言学习": "anki (subreddit:LearnJapanese OR subreddit:languagelearning OR subreddit:Korean)",
    "印度考试": "anki (subreddit:NEET OR subreddit:JEENEETards OR subreddit:Indian_Academia)",
}


def main():
    parser = argparse.ArgumentParser(description="统计 Anki 话题出现在哪些 Reddit 版块")
    parser.add_argument("--window", default="month", help="搜索时间窗：week / month / year")
    parser.add_argument("--pause", type=int, default=20, help="两次请求之间等几秒")
    parser.add_argument("--targeted", action="store_true",
                        help="改问几个候选人群用不用 Anki（按版块定向搜）")
    args = parser.parse_args()

    queries = list(TARGETED.values()) if args.targeted else QUERIES

    path = CONFIG if CONFIG.exists() else EXAMPLE
    config = json.loads(path.read_text(encoding="utf-8"))
    user_agent = config.get("user_agent", "anki-radar/1.0")

    counts = collections.Counter()
    samples = collections.defaultdict(list)
    seen = set()

    for index, query in enumerate(queries):
        if index:
            # 【必须歇】：连发几次就 429，而 429 之后整轮的结果都不可信。
            print(f"  （等 {args.pause} 秒）", flush=True)
            sources.pause(args.pause)
        print(f"搜索：{query}（{args.window}）", flush=True)
        try:
            items = sources.reddit_search(query, query, user_agent, window=args.window)
        except Exception as exc:  # noqa: BLE001
            print(f"  失败：{exc}", flush=True)
            continue
        fresh = 0
        for item in items:
            if item["external_id"] in seen:
                continue
            seen.add(item["external_id"])
            fresh += 1
            community = item["community"]
            counts[community] += 1
            if len(samples[community]) < 2:
                samples[community].append(item["title"][:70])
        print(f"  {len(items)} 条，其中新见到 {fresh} 条", flush=True)

    print()
    print(f"一共 {len(seen)} 条不重复的帖子，落在 {len(counts)} 个版块：")
    print()
    for community, n in counts.most_common():
        print(f"  {n:3d}  r/{community}")
        for title in samples[community]:
            print(f"       · {title}")


if __name__ == "__main__":
    sys.exit(main())

import os
from configparser import ConfigParser
from time import time
from typing import Sequence

from redis import StrictRedis
import ujson as json

from hortiradar.database import app, get_frog, get_keywords, insert_tweet, insert_lemma


if os.environ.get("ROLE") == "worker":
    keywords = get_keywords()
    keywords_sync_time = time()

    config = ConfigParser()
    config.read(os.path.dirname(__file__) + "/tasks_workers.ini")
    posprob_minimum = config["workers"].getfloat("posprob_minimum")

    redis = StrictRedis()
    rt_cache_time = 60 * 60 * 6


@app.task
def find_keywords_and_groups(id_str, text, retweet_id_str):
    """Find the keywords and associated groups in the tweet."""
    # refresh keywords
    global keywords, keywords_sync_time
    if (time() - keywords_sync_time) > 60 * 60:
        keywords = get_keywords()
        keywords_sync_time = time()

    # First check if retweets are already processed in the cache
    if retweet_id_str:
        key = "t:%s" % retweet_id_str
        rt = redis.get(key)
        if rt:
            kw, groups, tokens = json.loads(rt)
            insert_tweet.apply_async((id_str, kw, groups, tokens), queue="master")
            redis.expire(key, rt_cache_time)
            return

    frog = get_frog()
    # tokens contains a list of dictionaries with frog's analysis per token
    # each dict has the keys "index", "lemma", "pos", "posprob" and "text"
    # where "text" is the original text
    tokens = frog.process(text)
    kw = []
    groups = []
    for (i, t) in enumerate(tokens):
        lemma = t["lemma"]
        k = keywords.get(lemma, None)
        if k is not None:
            if t["posprob"] > posprob_minimum:
                if not t["pos"].startswith(k.pos + "("):
                    continue

            # when the second to last keyword is matched and the very last keyword is "…"
            # then we have a false match because the second to last keyword was truncated
            if i == (len(tokens) - 2):
                if tokens[-1]["text"] == "…":
                    continue

            kw.append(lemma)
            groups += k.groups
    kw, groups = list(set(kw)), list(set(groups))
    insert_tweet.apply_async((id_str, kw, groups, tokens), queue="master")

    # put retweets in the cache
    if retweet_id_str:
        data = [kw, groups, tokens]
        redis.set(key, json.dumps(data), ex=rt_cache_time)


@app.task
def lemmatize(key: str, texts: Sequence[str]):
    lemmas = []
    frog = get_frog()
    for text in texts:
        tokens = frog.process(text)
        lemmas.append(tokens[0]["lemma"])
    insert_lemma.apply_async((key, lemmas), queue="master")

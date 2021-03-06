from collections import Counter
from datetime import datetime, timedelta
import random

import numpy as np
from pattern.nl import sentiment

from hortiradar.clustering import Config, tweet_time_format
from .util import cos_sim, round_time, dt_to_ts, get_token_array
from hortiradar.database import obscene_words

max_idle = Config.getint('storify:parameters', 'max_idle')
threshold = Config.getfloat('storify:parameters', 'cluster_threshold')
original_threshold = Config.getfloat('storify:parameters', 'cluster_original_threshold')


class Stories:
    """
    Tracks clusters with similar content over longer time periods

    Parameters:
    max_idle:               Maximum amount of intervals that the story can be idle (no cluster is added)
    J_threshold:            Jaccard index threshold for adding cluster w.r.t. latest added tokens
    J_original_threshold:   Jaccard index threshold for adding cluster w.r.t. first added tokens

    Attributes:
    tokens:                 Counter of tokens that tokens are used in tweets
    original_tokens:        Counter of tokens that were added on creation
    filt_tokens:            Set of filtered tokens that were added last_edited
    original_filt_tokens:   Set of filtered tokens that were added on creation
    last_edited:            Number of iterations ago that a cluster was added
    tweets:                 Set of tweets corresponding to the story
    """

    def __init__(self, c, jt=None, ojt=None, mi=None, time=None):
        if not time:
            time = datetime.utcnow()

        self.id = dt_to_ts(time)
        self.created_at = round_time(time)
        self.origin = c.created_at
        self.last_edited = 0

        self.tokens = c.tokens
        self.filt_tokens = c.filt_tokens

        self.original_tokens = c.tokens
        self.original_filt_tokens = c.filt_tokens

        self.tweets = c.tweets
        self.retweets = c.retweets
        self.clusters = [c]
        self.first_tweet_time = min([tw.tweet.created_at for tw in self.tweets])

        self.max_idle = mi if mi else max_idle
        self.threshold = jt if jt else threshold
        self.original_threshold = ojt if ojt else original_threshold

    def __eq__(self, other):
        if type(other) == Stories:
            return self.id == other.id
        else:
            return False

    def is_similar(self, c):
        """Calculate if the cluster matches to the story"""
        all_current = self.filt_tokens & c.filt_tokens
        story_array = get_token_array(self.tokens, all_current)
        cluster_array = get_token_array(c.tokens, all_current)
        current = cos_sim(story_array, cluster_array)

        story_array = get_token_array(self.original_tokens, all_current)
        cluster_array = get_token_array(c.tokens, all_current)
        original = cos_sim(story_array, cluster_array)

        return current >= self.threshold and original >= self.original_threshold, current

    def add_cluster(self, c):
        self.tokens.update(c.tokens)
        self.filt_tokens.update(c.filt_tokens)
        self.clusters.append(c)
        self.tweets.update(c.tweets)
        for rid in c.retweets:
            if rid in self.retweets:
                self.retweets[rid] += c.retweets[rid]
            else:
                self.retweets[rid] = c.retweets[rid]
        self.first_tweet_time = min([tw.tweet.created_at for tw in self.tweets])

    def add_tweet(self, ext_tweet):
        if hasattr(ext_tweet.tweet, "retweeted_status"):
            rtid = ext_tweet.tweet.retweeted_status.id_str
            if rtid not in self.retweets:
                self.retweets[rtid] = []
            self.retweets[rtid].append(ext_tweet)
        else:
            self.tweets.update([ext_tweet])

    def add_delay(self):
        """Update idle time counter"""
        self.last_edited += 1

    def close_story(self):
        """Check if idle time has passed. If function returns true, then call endStory()"""
        return self.last_edited >= self.max_idle

    def end_story(self, time=None):
        """Add closing time to Story and calculate time series"""
        if not time:
            time = datetime.utcnow()
        self.closed_at = round_time(time)

    def get_timeseries(self):
        tsDict = Counter()
        for tw in self.get_tweet_list():
            tweet = tw.tweet
            dt = tweet.created_at
            tsDict.update([(dt.year, dt.month, dt.day, dt.hour)])

        ts = []
        if self.tweets:
            tsStart = sorted(tsDict)[0]
            tsEnd = sorted(tsDict)[-1]
            temp = datetime(tsStart[0], tsStart[1], tsStart[2], tsStart[3], 0, 0)
            while temp <= datetime(tsEnd[0], tsEnd[1], tsEnd[2], tsEnd[3], 0, 0):
                if (temp.year, temp.month, temp.day, temp.hour) in tsDict:
                    ts.append({"year": temp.year, "month": temp.month, "day": temp.day, "hour": temp.hour, "count": tsDict[(temp.year, temp.month, temp.day, temp.hour)]})
                else:
                    ts.append({"year": temp.year, "month": temp.month, "day": temp.day, "hour": temp.hour, "count": 0})

                temp += timedelta(hours=1)

        return ts

    def get_filtered_tweets(self):
        filt_tweets = []
        spam_list = []
        for tw in self.get_tweet_list():
            tweet = tw.tweet
            words = [t.lemma for t in tw.tokens]
            if any(obscene_words.get(t) for t in words):
                spam_list.append(tweet.id_str)
                continue
            else:
                filt_tweets.append(tw)

        return filt_tweets

    def get_best_tweet(self):
        story_array = get_token_array(self.tokens, self.filt_tokens)
        ext_tweets = [tw for tw in self.tweets]
        similarities = []
        for tw in ext_tweets:
            tweet_tokens = Counter(tw.tokens)
            tweet_array = get_token_array(tweet_tokens, self.filt_tokens)
            sim_value = cos_sim(story_array, tweet_array)
            if tw.tweet.id_str in self.retweets:
                sim_value *= np.sqrt(len(self.retweets[tw.tweet.id_str]))
            similarities.append(sim_value)

        if similarities:
            return ext_tweets[np.argmax(similarities)]
        else:
            return None

    def get_wordcloud(self):
        wordcloud = []
        for token in self.tokens:
            if token in self.filt_tokens:
                wordcloud.append({"text": token.lemma, "weight": self.tokens[token]})

        return wordcloud

    def get_locations(self):
        mapLocations = []
        for ext_tweet in self.get_tweet_list():
            try:
                if ext_tweet.tweet.coordinates is not None:
                    if ext_tweet.tweet.coordinates.type == "Point":
                        coords = ext_tweet.tweet.coordinates.coordinates
                        mapLocations.append({"lng": coords[0], "lat": coords[1]})
            except AttributeError:
                pass

        lng = 0
        lat = 0
        if mapLocations:
            for loc in mapLocations:
                lng += loc["lng"]
                lat += loc["lat"]
                avLoc = {"lng": lng / len(mapLocations), "lat": lat / len(mapLocations)}
        else:
            avLoc = {"lng": 5, "lat": 52}

        return {"locs": mapLocations, "avLoc": avLoc}

    def get_images(self):
        imagesList = []
        image_tweet_id = {}
        for ext_tweet in self.get_tweet_list():
            try:
                for obj in ext_tweet.tweet.entities["media"]:
                    url = obj["media_url_https"]
                    imagesList.append(url)
                    image_tweet_id[url] = ext_tweet.tweet.id_str
            except KeyError:
                pass

        images = []
        for (url, count) in Counter(imagesList).most_common():
            images.append({"link": url, "occ": count})

        return images

    def get_URLs(self):
        URLList = []
        for ext_tweet in self.get_tweet_list():
            try:
                for obj in ext_tweet.tweet.entities["urls"]:
                    url = obj["expanded_url"]
                    if url is not None:
                        URLList.append(url)
            except KeyError:
                pass

        urls = []
        for (url, count) in Counter(URLList).most_common():
            urls.append({"link": url, "occ": count})

        return urls

    def get_hashtags(self):
        htlist = []
        for ext_tweet in self.get_tweet_list():
            try:
                for obj in ext_tweet.tweet.entities["hashtags"]:
                    htlist.append(obj["text"])
            except AttributeError:
                pass

        hashtags = []
        for (ht, count) in Counter(htlist).most_common():
            hashtags.append({"ht": ht, "occ": count})

        return hashtags

    def get_original_wordcloud(self):
        wordcloud = []
        for token in self.original_filt_tokens:
            wordcloud.append({"text": token.lemma, "weight": self.tokens[token]})

        return wordcloud

    def get_polarity(self):
        lemmas = [t.lemma for t in self.tokens.elements()]
        polarity, subjectivity = sentiment(" ".join(lemmas))
        return polarity

    def get_interaction_graph(self):
        nodes = {}
        edges = []
        for ext_tweet in self.get_tweet_list():
            tweet = ext_tweet.tweet
            user_id_str = tweet.user.id_str
            if hasattr(tweet, "retweeted_status"):
                if tweet.retweeted_status.user.id_str:
                    rt_user_id_str = tweet.retweeted_status.user.id_str

                    if rt_user_id_str not in nodes:
                        nodes[rt_user_id_str] = tweet.retweeted_status.user.screen_name
                    if user_id_str not in nodes:
                        nodes[user_id_str] = tweet.user.screen_name

                    edges.append({"source": rt_user_id_str, "target": user_id_str, "value": "retweet"})

            if "user_mentions" in tweet.entities:
                for obj in tweet.entities["user_mentions"]:
                    if obj["id_str"] not in nodes:
                        nodes[obj["id_str"]] = obj["screen_name"]
                    if user_id_str not in nodes:
                        nodes[user_id_str] = tweet.user.screen_name

                    edges.append({"source": user_id_str, "target": obj["id_str"], "value": "mention"})

            if hasattr(tweet, "in_reply_to_user_id_str"):
                if tweet.in_reply_to_user_id_str:
                    if tweet.in_reply_to_user_id_str not in nodes:
                        nodes[tweet.in_reply_to_user_id_str] = tweet.in_reply_to_screen_name
                    if user_id_str not in nodes:
                        nodes[user_id_str] = tweet.user.screen_name

                    edges.append({"source": user_id_str, "target": tweet.in_reply_to_user_id_str, "value": "reply"})

        graph = {"nodes": [], "edges": []}
        for node in nodes:
            graph["nodes"].append({"id": nodes[node]})

        for edge in edges:
            source = edge["source"]
            target = edge["target"]
            graph["edges"].append({"source": nodes[source], "target": nodes[target], "value": edge["value"]})

        return graph

    def get_cluster_details(self):
        cluster_info = []
        for c in self.clusters:
            cluster_info.append(c.get_cluster_details())

        return cluster_info

    def get_tweet_list(self):
        """returns a list of all tweets in the Stories object"""
        tweets = [tw for tw in self.tweets.elements()]
        for rid in self.retweets:
            tweets += self.retweets[rid]

        return tweets

    def get_jsondict(self):
        """Builds the dict for output to JSON"""
        jDict = {}

        jDict["startStory"] = datetime.strftime(self.created_at, tweet_time_format)
        try:
            jDict["endStory"] = datetime.strftime(self.closed_at+timedelta(hours=1), tweet_time_format)
        except AttributeError:
            jDict["endStory"] = datetime.strftime(round_time(datetime.utcnow())+timedelta(hours=1), tweet_time_format)

        jDict["summarytweet"] = self.get_best_tweet().tweet.id_str

        jDict["cluster_details"] = self.get_cluster_details()

        jDict["tagCloud"] = self.get_wordcloud()
        jDict["polarity"] = self.get_polarity()

        tweetList = []
        interaction_tweets = []
        retweets = {}
        imagesList = []
        image_tweet_id = {}
        URLList = []
        htlist = []
        tsDict = Counter()
        mapLocations = []
        nodes = {}
        edges = []

        for tw in self.get_tweet_list():
            tweet = tw.tweet

            tweetList.append(tweet.id_str)

            dt = tweet.created_at
            tsDict.update([(dt.year, dt.month, dt.day, dt.hour)])

            user_id_str = tweet.user.id_str
            if hasattr(tweet, "retweeted_status"):
                if tweet.retweeted_status.user.id_str:
                    rt = tweet.retweeted_status
                    if hasattr(rt, "retweet_count"):
                        if rt.id_str not in retweets or rt.retweet_count > retweets[rt.id_str]:
                            retweets[rt.id_str] = rt.retweet_count
                    else:
                        if rt.id_str not in retweets:
                            retweets[rt.id_str] = 1
                        else:
                            retweets[rt.id_str] += 1

                    if rt.user.id_str not in nodes:
                        nodes[rt.user.id_str] = rt.user.screen_name
                    if user_id_str not in nodes:
                        nodes[user_id_str] = tweet.user.screen_name

                    edges.append({"source": rt.user.id_str, "target": user_id_str, "value": "retweet"})

            if "user_mentions" in tweet.entities:
                interaction_tweets.append(tweet.id_str)

                for obj in tweet.entities["user_mentions"]:
                    if obj["id_str"] not in nodes:
                        nodes[obj["id_str"]] = obj["screen_name"]
                    if user_id_str not in nodes:
                        nodes[user_id_str] = tweet.user.screen_name

                    edges.append({"source": user_id_str, "target": obj["id_str"], "value": "mention"})

            if hasattr(tweet, "in_reply_to_user_id_str"):
                if tweet.in_reply_to_user_id_str:
                    interaction_tweets.append(tweet.id_str)

                    if tweet.in_reply_to_user_id_str not in nodes:
                        nodes[tweet.in_reply_to_user_id_str] = tweet.in_reply_to_screen_name
                    if user_id_str not in nodes:
                        nodes[user_id_str] = tweet.user.screen_name

                    edges.append({"source": user_id_str, "target": tweet.in_reply_to_user_id_str, "value": "reply"})

            if tweet.entities.get("media"):
                for obj in tweet.entities["media"]:
                    image_url = obj["media_url_https"]
                    imagesList.append(image_url)
                    image_tweet_id[image_url] = tweet.id_str

            if tweet.entities.get("urls"):
                for obj in tweet.entities["urls"]:
                    url = obj["expanded_url"]
                    if url is not None:
                        URLList.append(url)

            if tweet.entities.get("hashtags"):
                for obj in tweet.entities["hashtags"]:
                    htlist.append(obj["text"])

            if hasattr(tweet, "coordinates"):
                if tweet.coordinates is not None:
                    if tweet.coordinates.type == "Point":
                        coords = tweet.coordinates.coordinates
                        mapLocations.append({"lng": coords[0], "lat": coords[1]})

        jDict["hashtags"] = []
        for (ht, count) in Counter(htlist).most_common():
            jDict["hashtags"].append({"ht": ht, "occ": count})


        jDict["tweets"] = list(set(tweetList))
        jDict["num_tweets"] = len(jDict["tweets"])

        jDict["timeSeries"] = []
        tsStart = sorted(tsDict)[0]
        tsEnd = sorted(tsDict)[-1]
        temp = datetime(tsStart[0], tsStart[1], tsStart[2], tsStart[3], 0, 0)
        while temp <= datetime(tsEnd[0], tsEnd[1], tsEnd[2], tsEnd[3], 0, 0):
            if (temp.year, temp.month, temp.day, temp.hour) in tsDict:
                jDict["timeSeries"].append({"year": temp.year, "month": temp.month, "day": temp.day, "hour": temp.hour, "count": tsDict[(temp.year, temp.month, temp.day, temp.hour)]})
            else:
                jDict["timeSeries"].append({"year": temp.year, "month": temp.month, "day": temp.day, "hour": temp.hour, "count": 0})

            temp += timedelta(hours=1)

        lng = 0
        lat = 0
        jDict["locations"] = mapLocations
        if mapLocations:
            for loc in mapLocations:
                lng += loc["lng"]
                lat += loc["lat"]
                jDict["centerloc"] = {"lng": lng / len(mapLocations), "lat": lat / len(mapLocations)}
        else:
            jDict["centerloc"] = {"lng": 5, "lat": 52}

        jDict["photos"] = []
        for (url, count) in Counter(imagesList).most_common():
            jDict["photos"].append({"link": url, "occ": count})

        jDict["URLs"] = []
        for (url, count) in Counter(URLList).most_common():
            jDict["URLs"].append({"link": url, "occ": count})

        # limit number of nodes/edges
        edges = random.sample(edges, min(len(edges), 250))
        connected_nodes = set([e["source"] for e in edges] + [e["target"] for e in edges])

        jDict["graph"] = {"nodes": [], "edges": []}
        for node in connected_nodes:
            jDict["graph"]["nodes"].append({"id": nodes[node]})

        for edge in edges:
            source = edge["source"]
            target = edge["target"]
            jDict["graph"]["edges"].append({"source": nodes[source], "target": nodes[target], "value": edge["value"]})

        # retweet ids sorted from most to least tweeted
        if retweets:
            jDict["retweets"], _ = zip(*sorted(filter(lambda x: x[1] > 0, retweets.items()), key=lambda x: x[1], reverse=True))
        else:
            jDict["retweets"] = []

        jDict["interaction_tweets"] = interaction_tweets

        return jDict

import sqlite3
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from analytics import compare_to_average, rank_papers, score_papers
from collector import (
    CollectionError, collect_public_reviews, group_settings, is_official_review,
    load_tables, parse_paper, parse_score, paper_status,
)


class FakeResponse:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self.data


class FakeSession:
    def __init__(self, pages, fail_after=None):
        self.pages = pages
        self.fail_after = fail_after
        self.calls = []

    def get(self, url, params, timeout):
        if url.endswith('/groups'):
            return FakeResponse({"groups": [{"content": {
                "submission_name": {"value": "Submission"},
                "review_name": {"value": "Official_Review"},
                "withdrawn_venue_id": {"value": "ICLR.cc/2027/Conference/Withdrawn"},
                "desk_rejected_venue_id": {"value": "ICLR.cc/2027/Conference/Desk_Rejected"},
            }}]})
        self.calls.append(params)
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise OSError('Network dropped mid-fetch')
        index = len(self.calls) - 1
        if index < len(self.pages):
            return FakeResponse({'notes': self.pages[index], 'count': 3})
        return FakeResponse({'notes': []})


def make_note(pid, number, rating, confidence, venue=None):
    return {
        'id': pid, 'number': number,
        'content': {
            'title': {'value': f'Paper {number}'},
            'primary_area': {'value': 'Geometry'},
            'venueid': {'value': venue or 'ICLR.cc/2027/Conference/Submission'},
        },
        'details': {'directReplies': [
            {
                'id': f'{pid}_r1', 'invitations': [f'ICLR.cc/2027/Conference/Submission{number}/-/Official_Review'],
                'content': {'rating': {'value': rating}, 'confidence': {'value': confidence}},
                'readers': ['everyone'], 'signatures': [f'ICLR.cc/2027/Conference/Submission{number}/Reviewer_one'],
            },
            {
                'id': f'{pid}_comment', 'invitations': [f'ICLR.cc/2027/Conference/Submission{number}/-/Official_Comment'],
                'content': {'rating': {'value': '4: Clear accept'}}, 'readers': ['everyone'],
            },
            {
                'id': f'{pid}_private', 'invitations': [f'ICLR.cc/2027/Conference/Submission{number}/-/Official_Review'],
                'content': {'rating': {'value': '1: Reject'}}, 'readers': ['Reviewers'],
            },
        ]},
    }


class TestExtraction(unittest.TestCase):
    def test_scores(self):
        self.assertEqual(parse_score({'value': '3: Weak Acceptance'}), 3.0)
        self.assertEqual(parse_score({'value': 4}), 4.0)
        self.assertEqual(parse_score('  2: weak reject'), 2.0)
        self.assertIsNone(parse_score('Confidence not available'))
        self.assertIsNone(parse_score(None))
        self.assertIsNone(parse_score(True))

    def test_official_only(self):
        note = make_note('a', 10, '3: Weak Acceptance', '4: Confident')
        settings = group_settings({'content': {}})
        paper, reviews = parse_paper(note, settings)
        self.assertEqual(paper[2], 'Paper 10')
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0][2], 3.0)
        self.assertEqual(reviews[0][3], 4.0)

    def test_status(self):
        settings = group_settings({'content': {}})
        self.assertEqual(paper_status(make_note('a', 1, 3, 4), settings), 'Active')
        self.assertEqual(paper_status(make_note('a', 1, 3, 4,
            'ICLR.cc/2027/Conference/Withdrawn_Submission'), settings), 'Withdrawn')
        self.assertEqual(paper_status(make_note('a', 1, 3, 4,
            'ICLR.cc/2027/Conference/Desk_Rejected_Submission'), settings), 'Desk rejected')

    def test_statistics_and_rank(self):
        papers = pd.DataFrame([
            ('a', 1, 'A'), ('b', 2, 'B'), ('c', 3, 'C'), ('d', 4, 'D'),
        ], columns=['paper_id', 'number', 'title'])
        reviews = pd.DataFrame([
            ('ra', 'a', 3, 4, 'R1'), ('rb', 'a', 2, 2, 'R2'),
            ('rc', 'b', 2, 3, 'R3'), ('rd', 'c', 2, 1, 'R4'),
        ], columns=['review_id', 'paper_id', 'rating', 'confidence', 'reviewer'])
        scored = score_papers(papers, reviews)
        a = scored.loc[scored.paper_id == 'a'].iloc[0]
        self.assertAlmostEqual(a.mean_rating, 2.5)
        self.assertAlmostEqual(a.weighted_rating, (3*4+2*2)/6)
        self.assertEqual(scored.loc[scored.paper_id == 'd', 'n_ratings'].iloc[0], 0)
        ranking = rank_papers(scored)
        self.assertEqual(ranking.iloc[0].paper_id, 'a')
        self.assertEqual(ranking.iloc[1]['rank'], 2)
        self.assertEqual(ranking.iloc[2]['rank'], 2)
        compare = compare_to_average(scored.mean_rating, 2)
        self.assertEqual((compare['ahead'], compare['tied'], compare['below']), (1, 2, 0))
        self.assertAlmostEqual(compare['percentile'], 100/3)

    def test_atomic_refresh_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            complete = Path(tmp) / 'complete.sqlite'
            working = Path(tmp) / 'working.sqlite'
            pages = [[
                make_note('a', 1, '3: Weak accept', '4: Confident'),
                make_note('b', 2, '2: Weak reject', '3: confident'),
            ], [make_note('c', 3, '4: Clear accept', 5)]]
            # First call finishes one page and fails on the next.
            with self.assertRaises(Exception):
                collect_public_reviews(session=FakeSession(pages, fail_after=1),
                    page_size=2, delay_seconds=0, complete_db=complete, working_db=working)
            self.assertFalse(complete.exists())
            self.assertTrue(working.exists())
            with sqlite3.connect(working) as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM papers').fetchone()[0], 2)
            # Resumed request sees the remaining page; it must advance cursor.
            result = collect_public_reviews(session=FakeSession([pages[1]]),
                page_size=2, delay_seconds=0, complete_db=complete, working_db=working)
            self.assertTrue(complete.exists())
            self.assertFalse(working.exists())
            self.assertEqual(result['total_submissions'], 3)
            papers, reviews, info = load_tables(complete)
            self.assertEqual(len(papers), 3)
            self.assertEqual(len(reviews), 3)
            self.assertEqual(info['complete'], '1')


if __name__ == '__main__':
    unittest.main()

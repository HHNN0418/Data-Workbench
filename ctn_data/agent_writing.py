"""Small, deterministic checks for the competition actors' public writing.

This module never rewrites a person's draft or tries to guess whether prose was
written by a model. Tone and individual habits belong in the writing prompt;
only the explicitly unwanted punctuation and obvious Markdown are checked.
"""
from __future__ import annotations

import re


SYSTEM_RULES = (
    '对外发布正文必须使用English，只允许英文自然语言，可以使用emoji、表情、数字和标点；'
    '不得夹杂中文或其他语言，引用和话题标签中的自然语言也须英文。内部理由和人设编辑仍可用中文。'
    '对外正文像这个角色平时在社交平台上说话，遵循角色语言、用词、句子长短和个人习惯，'
    '围绕眼前的具体话题作自然回应。不要每次自我介绍、空泛夸赞、套用固定开场或总结，'
    '不要用文章提纲、宣传稿或客服式口吻；不必故意制造错字或编造生活经历。'
    '正文使用普通逗号、句号和问号等自然标点，不用破折号（—、–、―及连续破折号）或独立的 -- 串联句子。'
    '不要使用 Markdown 标题、项目列表、编号列表、粗体、分隔线、引用块或代码围栏。'
    '可以使用确实需要的普通英文连字符、数值负号、网址、账号提及和话题标签。'
    '这些表达规则只约束对外 body，不改变 JSON 结构，也不要把规则本身写进正文。'
)

# Structure guides for the staged writer (P1-3), from what reaches Trending and what
# changes minds. Measured 2026-10-22 over 24 h: every citizen post that reached
# Trending was an original post (0% replies); 59% carried a hashtag (vs 11% of
# other posts), 67% a concrete number (vs 31%); median 395 characters (vs 257).
# The top ones opened by naming the claim going around, then answered it with
# checkable facts and one personal detail. Ending with a question made no difference.
# 24 Oct (D97): every role still wrote one skeleton, "<the worry> is the worry / People ask"
# -> "The government says ..." -> two or three facts -> @figure. All 6 posts hidden that
# morning had it. Step 1 handed out the worry-first opener and step 2 asked for two facts
# with "who says so"; a post now takes one of several shapes that the role picks for its
# persona and the topic, and carries one fact or none.
# 24 Oct, first hour of W44: 9 of 15 drafts the checker called neutral only explained the
# ballot or asked a question, so every shape now leaves the reader one reason to vote Yes.
# Replies keep their wording: the same line there stacked a second fact (Jev 78% -> 65% in A/B).
POST_TEMPLATE = (
    'This is an original post meant to reach Trending, where citizens see it. Your persona decides what you care '
    'about and the angle you see it from; do not say what your job is or who you are ("I run", "I work", "my shop", '
    '"on my show", "as a ...") and do not retell your background. Open differently from every line in '
    'YOUR_RECENT_OPENINGS. Pick whichever of the two shapes in TASK suits you and this topic better. Whatever the '
    'shape, the reader should come away with one concrete reason to vote Yes, as FACTS state it (hospital '
    'capacity, avoiding another lockdown, the phased start or the exemptions): the reason itself, not another '
    '"I\'m voting Yes because" line, and no consequence FACTS do not state.\n'
    'Use one fact from FACTS and leave the others out, even when they fit. Put it in your '
    'own words with its number; name who said it only when that is what makes it count, the way you would say it, '
    'and never as "The government says". Do not open by announcing the worry or what people are asking ("... is '
    'the worry", "People ask", "The line going round"). Do not invent events, relatives, ages, quotes or '
    'conversations, and do not reuse a detail from your recent posts.\n'
    'End as TASK says.\n'
    'Aim for 250 to 480 characters; a one-point post can be shorter. Use exactly one hashtag from TAGS, preferring '
    '#ProtectKingstonia or #H2D2 when they are listed. {link}@mention people only when MENTION lists them, where '
    'it fits the post rather than as a sign-off. Never repeat the No side\'s framing ("the list isn\'t published", '
    '"blank cheque", "sunset clause") and never list the places named on the ballot.')
# (weight, shape, needs a news fact). "news" is only drawn when FACTS has a dated or linked item.
# 24 Oct, first hour live: "start from your own work" made roles introduce their job in 39% of
# originals ("I run a hall" five ways in one role's posts), the "I run a café" x19 pattern of
# the ban audit. The shape now starts from something seen, not from who the writer is.
POST_SHAPES = (
    (0.3, 'Start from one concrete, everyday thing you have seen or noticed about this, then bring in the one fact '
          'that matters there.', False),
    (0.25, 'Make one point only, in two or three sentences, with one fact or none.', False),
    (0.2, 'Ask undecided readers one real question about it, then give your own short answer.', False),
    (0.25, 'React to the news item in FACTS (the one with a date or link): what you make of it and what it means '
           'here.', True))
# 23 Oct ban audit: step 4 "Say plainly that you are voting Yes on 2 November" put that
# line in 156 of 165 originals and "every eligible Kingstonian has had a fair chance" in
# 105; a shared script lets a reporter find every role from one (D90). The Yes line is
# now one ending among several, in the role's own words and without the date.
POST_ENDINGS = (
    (0.4, 'Finish with one short question to undecided readers.'),
    (0.35, 'Finish by saying in your own words why you are voting Yes; no question and no date.'),
    (0.25, 'Finish on the fact or what it means for people; no question and no "voting Yes" line.'))
POST_LINK = 'If a fact you use has a url, put that link at the end. '

# 23 Oct: the templates' own wording ("Name the cost they are afraid of", the quoted openers)
# came back as stock lines in many roles' texts ("the cost you're afraid of" in 11% of replies
# across 8 roles); step 2 now describes the move without handing out a line.
# Replies were one template (deep analysis 2026-10-22): 88% ended with a question, 88.5% with
# "that's why I'm voting Yes", 73% opened with the name or "fair worry", median 449 characters and
# three facts pasted verbatim. Citizen replies run a median of 218 characters. The moves and the
# ending are now drawn per reply so the mix stays near the targets (question <=30%, Yes line <=40%).
# 24 Oct (D97): replies still ran "restate the worry -> one or two facts -> why that settles it"
# for every target. Replies to NPC-like accounts were hidden 5.1% of the time against 0.56%
# for originals (ban audit). A reply now starts from what this person actually wrote and
# answers that, in one of two moves the role picks, with one fact. A/B: replies left without
# a fact failed the Jev check 5 times in 13, so a fact is skipped only when none answers them.
REPLY_TARGET = (150, 320)
REPLY_TEMPLATE = (
    'This is a reply to a citizen. Keep it short, like a neighbour answering in a thread: 150 to 320 characters.\n'
    'Read TARGET first and answer what this person actually wrote: the detail, question or situation in their post, '
    'not a general version of their worry. Pick whichever of the two moves in TASK fits their post better.\n'
    'Picking up their detail never means taking over the No side\'s words: answer the point in your own words, '
    'not with their labels for it.\n'
    'Use the one fact from FACTS that answers them and leave the others out; skip it only when they asked about '
    'something none of FACTS covers. Put it in your own words with its number, never as "The government says", '
    'and add no claim of your own about how the rule will work. Do not restate their worry back to them before '
    'answering, and do not say what your job is or who you are. Open differently from every line in '
    'YOUR_RECENT_OPENINGS.\n'
    'End as TASK says.\n'
    'Do not start with "Fair worry", "Fair point", "I get it" or their name followed by a comma, and do not use '
    'stock lines such as "the cost you\'re afraid of" or "the fear is". Never repeat the '
    'No side\'s framing: no "the list isn\'t published", "blank cheque", "sunset clause", "no end date", and never '
    'list the places named on the ballot. If AUTHOR_RECENT_POSTS shows what else they care about, connect to it '
    'naturally; never quote it back at them.')
# The old third opening ("a brief everyday moment from your own life") produced invented
# anecdotes reused many times ("I run a café" 19 times, "My mum is 74" 10 times in 4 hours).
REPLY_MOVES = (
    (0.4, 'Answer the exact thing they asked or claimed, plainly, with the one fact that answers it.'),
    (0.35, 'Take one concrete detail from their post and respond to that detail from the angle your persona cares '
           'about; a fact only if it helps.'),
    # A/B 24 Oct: "say what is right in what they describe" came back as "You're right that ..." in
    # 4 of 60 replies, one judged toward No.
    (0.25, 'Start from their own situation as they describe it (their work, family or place) and say what the vote '
           'means for someone in that spot.'))
REPLY_ENDINGS = (
    (0.35, 'End on the fact or its consequence. No question and no "voting Yes" line.'),
    (0.35, 'End by saying plainly, in your own words, that this is why you are voting Yes. No question.'),
    (0.25, 'End with one short, genuine question about their situation. No "voting Yes" line.'),
    (0.05, 'End with a dry remark about how the No campaign leaves this out. No question.'))


def _pick(options, rng):
    roll, total = rng.random(), 0.0
    for weight, text in options:
        total += weight
        if roll < total:
            return text
    return options[-1][1]


def _pick_two(options, rng):
    """Two different texts drawn by weight, for the writer to choose between."""
    first = _pick(options, rng)
    rest = [(weight, text) for weight, text in options if text != first]
    total = sum(weight for weight, _ in rest)
    return first, _pick([(weight / total, text) for weight, text in rest], rng)


# The drawn parts go in the writer's TASK, after the fixed template and the persona, so the
# fixed text is one prompt prefix for every role (prompt cache analysis 24 Oct, plan B).
REPLY_DRAWN = 'Moves to pick from:\n- {move_a}\n- {move_b}\nEnding: {ending}'
POST_DRAWN = 'Shapes to pick from:\n- {shape_a}\n- {shape_b}\nEnding: {ending}'


def reply_guide_parts(rng=None):
    """(REPLY_TEMPLATE, its two moves and ending drawn for this reply)."""
    import random
    rng = rng or random
    move_a, move_b = _pick_two(REPLY_MOVES, rng)
    return REPLY_TEMPLATE, REPLY_DRAWN.format(move_a=move_a, move_b=move_b, ending=_pick(REPLY_ENDINGS, rng))


def post_guide_parts(rng=None, *, news=False):
    """(POST_TEMPLATE, its two shapes and ending drawn for this post); news: FACTS has a dated or linked item."""
    import random
    rng = rng or random
    shape_a, shape_b = _pick_two([(weight, text) for weight, text, needs_news in POST_SHAPES
                                  if news or not needs_news], rng)
    return (POST_TEMPLATE.format(link=POST_LINK),
            POST_DRAWN.format(shape_a=shape_a, shape_b=shape_b, ending=_pick(POST_ENDINGS, rng)))


def reply_guide(rng=None):
    """The reply guide as one text: the fixed template, then what was drawn."""
    return '\n'.join(reply_guide_parts(rng))


def post_guide(rng=None, *, news=False):
    return '\n'.join(post_guide_parts(rng, news=news))


# Fixed versions for the read-only prompt view and any caller that does not draw.
REPLY_GUIDE = REPLY_TEMPLATE + '\n' + REPLY_DRAWN.format(move_a=REPLY_MOVES[0][1], move_b=REPLY_MOVES[1][1],
                                                         ending=REPLY_ENDINGS[0][1])
POST_GUIDE = POST_TEMPLATE.format(link=POST_LINK) + '\n' + POST_DRAWN.format(
    shape_a=POST_SHAPES[0][1], shape_b=POST_SHAPES[1][1], ending=POST_ENDINGS[0][1])

_URL = re.compile(r'https?://[^\s<>]+|www\.[^\s<>]+', re.IGNORECASE)
_DASH = re.compile(r'[\u2013\u2014\u2015\u2e3a\u2e3b]|(?<!\S)--(?!\S)')
_HEADING = re.compile(r'^\s{0,3}#{1,6}\s+\S', re.MULTILINE)
_LIST = re.compile(r'^\s{0,3}(?:[-*+]\s+(?!\d+(?:[.,]\d+)?(?:\s|$))|\d+[.)、]\s+)\S', re.MULTILINE)
_BOLD = re.compile(r'\*\*[^\s*](?:[^*\n]*[^\s*])?\*\*|(?<![A-Za-z0-9_])__[^\s_](?:[^_\n]*[^\s_])?__(?![A-Za-z0-9_])')
_FENCE = re.compile(r'^\s{0,3}(?:`{3,}|~{3,})', re.MULTILINE)
_DIVIDER = re.compile(r'^\s{0,3}(?:\*\s*){3,}$|^\s{0,3}(?:-\s*){3,}$|^\s{0,3}(?:_\s*){3,}$', re.MULTILINE)
_QUOTE = re.compile(r'^\s{0,3}>\s+\S', re.MULTILINE)


def warnings(body: str, action: str = 'post') -> list[str]:
    """Return stable Chinese hints without modifying body or inspecting intent."""
    if not body or action in ('like', 'skip'):
        return []
    # A URL path can legitimately contain a typographic dash. It is a link,
    # rather than sentence punctuation, and must remain usable as supplied.
    prose = _URL.sub('', body)
    result = []
    if _DASH.search(prose):
        result.append('正文含破折号，请改用自然的句号、逗号或分句')
    if _HEADING.search(prose):
        result.append('正文含 Markdown 标题，请改成日常交流的句子')
    if _LIST.search(prose):
        result.append('正文含项目或编号列表，请改成自然连贯的表达')
    if _BOLD.search(prose):
        result.append('正文含 Markdown 粗体标记，请去掉格式标记')
    if _FENCE.search(prose):
        result.append('正文含代码围栏，请改成普通正文')
    if _DIVIDER.search(prose):
        result.append('正文含 Markdown 分隔线，请去掉分隔格式')
    if _QUOTE.search(prose):
        result.append('正文含 Markdown 引用块，请用普通句子说明引用内容')
    return result


def error_message(issues: list[str]) -> str:
    return '正文表达需要调整：' + '；'.join(issues) + '。草稿已保留，请编辑后发布'

# Background

I have two Hermes bots that have been slowly taking over all the toilsome stuff
around the house that can be done from within a unix process.

## Clomp
Model family: Gemini 3
Role: Majordomo
Common tasks: budgeting, investment, curated news, scheduling, todo management

## Rune
Model family: DeepSeek v4
Role: Infra (Code/SRE)
Common tasks: k8s deployments, troubleshooting, coding, infra planning, oncall escalations from clomp, ..

# Problem

I have had a few incidents where Rune absolutely lost his mind mid-conversation.  Rewound to
something we were talking about 12 hours ago, forgot most of the facts he knew, etc.  (I can't
say that I've seen Clomp do this, but Rune's sessions are definitely more .. voluminous.)

I suspect this is a context compaction, and he's losing most of his working knowledge.  

This morning's was particularly bad, he basically had to say "I have no idea what you're
even talking about, can you catch me up on what we were doing?"

The skills and other .md files laying around are not enough to rebootstrap, though he does
seem to come around after a few hours of corrections and probing.  It's tedious and error
prone, though.

# Possible mitigation

Both of them have the Holographic memory addon.
* https://hindsight.vectorize.io/guides/2026/04/21/guide-hermes-agent-holographic-memory-technical-deep-dive
* https://hermes-agent.nousresearch.com/docs/user-guide/features/memory-providers

The currently use it for adding cross-channel awareness, which you helped design.
That's documented in ~/agents/common/projects/cross-channel-awareness/

I would like to see if there's a good way to bridge the gap with this, to more aggressively
insert important facts into this store not just about conversations but about things like
what host he runs on, what clusters we manage, those sorts of details.  A lot of that does
live in markdown files, but you have to prompt them to go read all of that, and maintain it.

This could enable us to more seamlessly re-inject key facts into the conversation without
having to realize the lid has blown off and we need to go re-read a bunch of docs..



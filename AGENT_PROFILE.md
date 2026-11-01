# Mori Together

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

**Turn “we should hang out sometime” into a plan you both say yes to.**

Mori helps two friends discover activities, restaurants, parks and places to explore in Berkeley and San Francisco. Share your preferences in English or Chinese, review options, and approve a plan together.

## Start naturally

Say “create a plan”, “help us plan an outing”, or “帮我们安排一次出门”. You can also start with what you want to do, such as “Find music events this weekend under $30 per person”. Mori creates a plan and keeps the preferences you have supplied.

Share the generated invite code with your friend, who sends it to Mori from their own conversation. Names are optional and can be added in the card. In a group channel, **@mention Mori for each message meant for it**. In our tests, unmentioned messages were not delivered; Mori does not silently read the whole conversation.

## Pick times, then explore

Choose up to three available time slots from the card's dropdowns: morning, midday, afternoon or evening over the coming seven days. No timestamps to type. You can also describe custom time windows in natural language. Times are Pacific and account for daylight saving time.

Once both friends provide their city, time, budget and interests, Mori automatically searches their actual overlapping windows. Ticketmaster supplies activities; Google Maps supplies restaurants and places, with photos when available.

Choose an option and have both participants approve the exact proposal. Browsing alternatives keeps the confirmed plan; proposing a replacement requires fresh approval from both people. Changing preferences resets the proposal and approvals.

## Calendar and destination links

After both approve, open a calendar draft and the map or event link. Save the calendar entry yourself. These links can be opened repeatedly. Mori does not buy tickets or make reservations.

Listed event prices exclude unverified fees. For places and restaurants, check opening hours, costs, accessibility and booking requirements yourself. A suggested visit time is not confirmed availability; an unknown cost is not free. Restaurant/place windows must be six hours or less.

## Your information and control

Preferences are visible to your planning partner. Share only what you want them to see. This prototype distinguishes transport sessions, not verified human identities. It does not import chat history or send unsolicited reminders.

Use `help` for instructions, `edit` to change your preferences, `forget` to clear them, or `leave` to leave a plan. The creator can `delete` the shared plan. Legacy `new YourName`, `join CODE YourName`, and `search` commands remain supported, but are not required for ordinary planning.

Built with Fetch.ai uAgents, Agent Chat Protocol, Agentverse, ASI:One language understanding and cards, Ticketmaster, and Google Places.

[Source code and setup](https://github.com/Fan-Runming/mori-together)

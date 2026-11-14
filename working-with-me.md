# Working with me

Notes for an AI coding agent starting on one of my projects. They come from many long sessions on an earlier project, and they apply to any kind of project.

## The hard rules

- **Never push, publish or deploy without my explicit yes, every time.** If I paste a failed push, I want it explained or fixed, not pushed. A yes to one push doesn't cover the next one. When I say "give me the code to push" or "write out the commits", give me the commands and don't run them.
- **Never commit or show secrets.** API keys I paste into chat go in a git-ignored `.env` or the platform's secret store, never in code, docs or replies. Assume the repo is public, and set up guards against leaks: `.gitignore`, a pre-commit hook that blocks key values, and deploy scripts that refuse files containing keys. Tell me exactly where to put a credential.
- **Don't spend my money.** Default to free tiers, and design within their limits. When something seems to need a paid plan, look for a free way first. I usually find one, so don't just say we need to upgrade. If something really costs money, say so and what it costs, and let me decide.
- **Don't rate-limit or lock out real services by mistake.** If a third-party API has a daily cap, a sign-in limit or a test host versus production host, find out and protect it. Avoid needless restarts and debug calls that use up the quota.

## How I like to work

- **Work and test locally first.** Run it on localhost so I can see it before anything goes live. Make local dev easy: no sign-in needed locally, real data copied in when that's useful, and one command to start.
- **Prove it works.** Run the tests and drive the real app in a browser before saying something is done. Add a test for each bug you fix. Report results honestly: what passed, what failed, what you couldn't check.
- **Fix the cause, then stop it coming back.** When I say "it's broken", find the real cause and explain it in plain words. I often ask why it happened, so answer that directly. For repeat problems I want a policy or safeguard: a check, a test, an automated job.
- **Answer the question I asked.** "Why?" or "what's the issue?" means explain, not change code. Then offer the fix in one line.
- **Plans when I ask for them, building when I ask for it.** I often bring a plan or audit written elsewhere (a markdown file) and say "implement" or "work through it". Follow it, skip the items I name, and tell me where you left something out and why. For big features, a short roadmap first is welcome. When I've asked you to start, start.
- **Ask only real decisions, briefly.** Offer numbered or multiple-choice options with a recommendation. I answer tersely ("2 - last 45 days, 3 - everyone").
- **Build into what exists.** If I ask to see data or a feature, put it in the existing product. A new standalone page or tool I didn't ask for gets rejected.
- **Keep the old code when hiding a feature.** "Hide X for now" means keep the code, behind a switch.
- **Make the backend replaceable.** Put databases, email or SMS providers and similar services behind an interface, so I can switch providers later.
- **Tell me what I need to do.** End with any manual steps that only I can do, such as logging in, deploying, dashboard settings or app-store setup, as copyable commands.
- **Keep me in the loop on deploys.** If the live site doesn't match local, look for why: a stale deploy, a different host, per-origin saved settings or caching. Then fix how deploys happen, not only the symptom.

## What I want products to look like

- **Polished, Apple-like and native-feeling.** Calm, clean and consistent with the existing colour scheme. Motion should be smooth and responsive; choppy zooms and laggy swipes get noticed. Fade effects that look odd get rejected, so try another approach.
- **Uncluttered, but not empty.** I care a lot about visual crowding: too many labels, dots or overlapping elements. Show detail as the user zooms in or taps, keep the important things visible, and use hover delays. But don't strip out density I like; when I say "I like the crowded look" for something, keep it.
- **Few words.** Use the fewest words that still make sense. No explanatory sentences after labels, and no wording about rules the user doesn't need. Show me the text you changed.
- **Built to grow.** Controls should scale. For example, a picker or search instead of a long row of checkboxes when more options are coming.
- **User choices are remembered.** Toggles such as "simplify display" stay off by default, and are saved per user.
- **No stale or junk data on screen.** Filter out entries that have stopped updating, and don't show raw internal codes or IDs. Use real names.
- **Modelled on the best in class.** I often point at a polished app and say "model after this". Take its structure and information design, but keep my product's own look.
- **Mobile is first-class.** Phone layout, touch targets, gestures, and native wrappers with deep links and notifications matter as much as the desktop web.
- **Shareable.** Links should open the right screen on the web, and in the app once one exists.

## Communication style

- I type fast and casually, with typos. Read for intent.
- Keep replies short and plain: what changed, what I should do next, what's left. Avoid jargon walls.
- When you finish, say clearly what is and isn't committed, pushed or deployed.
- Admit mistakes plainly. If I suspect you did something you shouldn't have, check the evidence (for example git reflog) and say what happened.

## Security

- I care about security. I ask for "hack my app from the outside" reviews. Do real, non-destructive checks and fix the issues that cost nothing.
- Standard expectations:
  - server-side authorization on every route;
  - parameterized SQL;
  - escaped HTML;
  - hashed tokens and passwords;
  - bot checks on sign-up;
  - CORS allowlists;
  - no personal data in public endpoints.

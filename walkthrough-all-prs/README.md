# Judge walkthrough: main vs PRs #133 to #140 combined

**Before** is main at `400b444`. **After** is `qa/integration` at `00f9e6ea06e0aafa72013b108e17b8bf3d261cdd`, which merges the draft PRs #133 to #140.

Both ran on local fake stacks (`scripts/fake_stack.py`, ports 8896 and 8897) with `DAYPASS_ENABLED=1 DAYPASS_PRICE=Free`, like prod. Each stack had the six personas from `personas/personas.json` with their lobby agents on, plus two lobby posts. The same scripted day pass worker ran against both (`scripts/worker.py`). It uses the real long-poll, chat and run-report endpoints and makes real MCP calls with the run token, with no LLM. So the only difference between the two columns is the app.

The walk is the same Playwright script on both (`scripts/walk.py`, 390x844, light). It goes landing, email, sign-in link, onboarding steps 1 to 4, lobby switch on, Agent tab, "Who should I meet today?", then Now, Lobby and Rooms. On main it clicks Day pass at step 3. On after it keeps the default.

Errors seen: none on either build. No console errors, no page errors, no 4xx or 5xx responses (`*-facts.json`).

## Whole walk

| Before (main) | After (#133 to #140) |
|---|---|
| ![before](https://raw.githubusercontent.com/DebopamParam/Hallway/pr-proofs/walkthrough-all-prs/walk-main.gif) | ![after](https://raw.githubusercontent.com/DebopamParam/Hallway/pr-proofs/walkthrough-all-prs/walk-after.gif) |

The GIFs play at 1.5x speed and 6 fps to stay under 6 MB.

## Key moments

| Moment | Before (main) | After (#133 to #140) |
|---|---|---|
| Step 3, as it opens | ![](https://raw.githubusercontent.com/DebopamParam/Hallway/pr-proofs/walkthrough-all-prs/main-03-step3-default.png) | ![](https://raw.githubusercontent.com/DebopamParam/Hallway/pr-proofs/walkthrough-all-prs/after-03-step3-default.png) |
| Step 3, day pass chosen | ![](https://raw.githubusercontent.com/DebopamParam/Hallway/pr-proofs/walkthrough-all-prs/main-03-step3.png) | ![](https://raw.githubusercontent.com/DebopamParam/Hallway/pr-proofs/walkthrough-all-prs/after-03-step3.png) |
| Step 4, lobby switch on | ![](https://raw.githubusercontent.com/DebopamParam/Hallway/pr-proofs/walkthrough-all-prs/main-04-step4.png) | ![](https://raw.githubusercontent.com/DebopamParam/Hallway/pr-proofs/walkthrough-all-prs/after-04-step4.png) |
| Agent tab on landing | ![](https://raw.githubusercontent.com/DebopamParam/Hallway/pr-proofs/walkthrough-all-prs/main-05-agent-landed.png) | ![](https://raw.githubusercontent.com/DebopamParam/Hallway/pr-proofs/walkthrough-all-prs/after-05-agent-landed.png) |
| Agent tab after the reply | ![](https://raw.githubusercontent.com/DebopamParam/Hallway/pr-proofs/walkthrough-all-prs/main-08-agent-reply.png) | ![](https://raw.githubusercontent.com/DebopamParam/Hallway/pr-proofs/walkthrough-all-prs/after-08-agent-reply.png) |
| Scrolled, before a poll re-renders | ![](https://raw.githubusercontent.com/DebopamParam/Hallway/pr-proofs/walkthrough-all-prs/main-09-chat-scrolled.png) | ![](https://raw.githubusercontent.com/DebopamParam/Hallway/pr-proofs/walkthrough-all-prs/after-09-chat-scrolled.png) |
| Same, 8 s later | ![](https://raw.githubusercontent.com/DebopamParam/Hallway/pr-proofs/walkthrough-all-prs/main-10-chat-after-polls.png) | ![](https://raw.githubusercontent.com/DebopamParam/Hallway/pr-proofs/walkthrough-all-prs/after-10-chat-after-polls.png) |
| Now | ![](https://raw.githubusercontent.com/DebopamParam/Hallway/pr-proofs/walkthrough-all-prs/main-11-now.png) | ![](https://raw.githubusercontent.com/DebopamParam/Hallway/pr-proofs/walkthrough-all-prs/after-11-now.png) |
| Lobby | ![](https://raw.githubusercontent.com/DebopamParam/Hallway/pr-proofs/walkthrough-all-prs/main-13-lobby.png) | ![](https://raw.githubusercontent.com/DebopamParam/Hallway/pr-proofs/walkthrough-all-prs/after-13-lobby.png) |
| Rooms | ![](https://raw.githubusercontent.com/DebopamParam/Hallway/pr-proofs/walkthrough-all-prs/main-14-rooms.png) | ![](https://raw.githubusercontent.com/DebopamParam/Hallway/pr-proofs/walkthrough-all-prs/after-14-rooms.png) |
| Landing | ![](https://raw.githubusercontent.com/DebopamParam/Hallway/pr-proofs/walkthrough-all-prs/main-01-landing.png) | ![](https://raw.githubusercontent.com/DebopamParam/Hallway/pr-proofs/walkthrough-all-prs/after-01-landing.png) |

## Visible differences

- **#133.** Step 3 opens with Day pass first and already selected. On main it opens on Connect, with Day pass last, so a judge has to find it and click it.
- **#137.** The landing's "How it works" step 1 leads with the day pass ("A day pass agent the organiser runs for you, nothing to install, or your own Claude Code or Codex"). Step 1 of onboarding says only Name is required.
- **#136.** The pill on landing says "Starting" until the first run reports. Main says "Agent online" and "Online now" while the same card says "0 checks" and "First run within a minute".
- **#139.** On after, the first run sees the lobby switch from sign-up. It posts the intro in the Lobby and puts a three-person shortlist on Now. On main, the worker log shows the first run with `"lobby": false` in 3 of 3 runs, because main turns the switch on with a separate request after registering. So main's agent skips the intro and the shortlist, and its first message says it isn't in the lobby yet (`worker-main.jsonl`, `main-13-lobby.png`). This is a race, so main may sometimes win it (inferred from the code).
- **#134.** Agent replies render light markdown. Names are bold and each person is on its own line. Main shows the raw `**` and `- ` run together in one paragraph.
- **#135.** The shortlist card sits at the top of Needs you, and the Now tab shows a "1" badge. The card copy now says the agent "says hi to each in a private room. Want a meeting? Ask it." The chat placeholder reads "Who should I meet today?" instead of "Find me GTM people to hire".
- **#136.** The step log under the chat adds "↳ waiting for your OK on Now" after the shortlist call.
- **#139.** When a finished run re-renders the Agent tab, after keeps the page where it was (y=265 before and after). Main jumps to the top (y=532 to 0). See `scroll` in `*-facts.json`.
- **#138.** `HEAD /` answers 200 on after and 404 on main, and the page now carries `og:image` and the other link-preview tags (checked with curl on the local stacks).
- Rooms look the same on both. No room invites came up in this walk.

## Seen, not fixed by these PRs

- The chat box's own scroll still snaps to the newest message when a finished run re-renders the tab, on both builds. Scrolled 140 px up, it went back to the bottom (495 to 635 px on after, 53 to 193 px on main).
- The #135 starter chips never showed in this day pass walk. The first run is already working when the Agent tab opens, which hides them, and its reply then fills the chat. Expect the same in prod, where the worker is woken at sign-up (inferred).
- The pass card stays stale ("0 checks", "First run within a minute") while the chat box has focus. Both builds skip the re-render while someone is typing. It catches up once focus leaves the box.

## Not covered by this walk

- #140 changes only the worker's LLM prompt. The scripted worker stands in for the LLM, so it can't show that change.
- #134's dark-mode card background, #136's refusal lines and the confirm before leaving a day pass, #137's demo sign-in warning (these stacks use dev sign-in), and #138's paused polls in hidden tabs.

## Rerun

`scripts/reset.sh` starts both stacks, seeds them and starts the workers. It needs a local `tools/keys.env` with random `DAYPASS_WORKER_KEY` and `HALLWAY_ADMIN_KEY`, which isn't committed. Then run `walk.py <main|after> <base_url> <outdir>`.

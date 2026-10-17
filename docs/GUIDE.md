<!--
GUIDE.md v0.13 — 2026-10-17
Changelog (latest two; older entries: `git log -- RouterLM/impl/GUIDE.md`):
- v0.13 (2026-10-17): Part 2 installs with one pasted line that starts RouterLM with the Mac; update and remove lines; Linux keeps the start line (Rene: "slice 7 is the install line").
- v0.12 (2026-10-15): No FileMaker examples left: the persona, the Part 1 diagram and the full-web-address line (Rene).
Staged copy of RouterLM/impl/GUIDE.md; edit that one and stage again.
-->

# RouterLM guide

## Who it's for

Claude's wording, approved by Rene 2026-10-15. Every label on the screens is written for #1 and #3. If they
wouldn't know a word, it goes or gets one plain sentence.

1. **The would-be builder (primary).** Uses Claude or ChatGPT every day for work. Has hooked one thing up to an
   AI model by following a tutorial: a script or a small app. Can sign up for an account, copy
   an API key and fill in a settings page. Doesn't use the terminal and shouldn't have to; "port", "hash" and
   "JSON" mean nothing to them. The pain: whatever they built goes dead when the provider has an outage, the card
   on the account expires or the internet drops. Wants it running in an afternoon, and wants to know what happens
   when the Primary can't answer.
2. **The tinkerer.** Has LM Studio or Ollama on a Mac and likes trying models. Knows more than #1, but it isn't
   their job. Wants local models and cloud keys in one place, in the order they pick, and to see which one
   answered.
3. **The owner who had it set up.** Their office runs on something a consultant or the office's tech person
   built. They're one of the "3 who care". They open the page to see that it's working and which model is
   answering, and maybe change the order. They never touch the setup.
4. **The person who sets it up for others.** A consultant or the office's go-to tech person. Installs it once,
   builds the lineups and hands it over. Needs the page to explain itself so #3 can read it without calling them.
5. **The people in the office.** They never open RouterLM; their app just keeps answering.

The engineer isn't one to design for. They get the raw settings file at the bottom of Settings and the spec on
GitHub.

## The names used here

One name per thing, and the same name on every screen.

| Name | What it is |
|---|---|
| **Model** | An AI model you can use: Claude Opus 5.5, GPT-5.5, Qwen 27B on the office Mac. Cloud or local, it's a model. |
| **Cloud, Local** | A cloud model runs at a model company. A local model runs on a machine in the building. Local means in the room; it doesn't by itself mean private or secure. |
| **API key** | The code from your account with a model company (Anthropic, OpenAI) that lets RouterLM use its models. One API key covers all of that company's models. Local models need none. An app on the RouterLM Mac needs no API key; RouterLM makes one for an app on another computer. |
| **Local AI app** | A program that runs models on your own machine: LM Studio, Ollama, llama-server. It runs models; it isn't one. |
| **Lineup** | Models in order. #1, the Primary, answers; if it can't, #2 does, and so on down. You decide the order and where it ends. You can have more than one. |
| **App** | A program that sends questions to RouterLM: Capsule, a script, a chat window. |
| **Local only** | A lineup or an app marked Local only uses local models only, backups included. Nothing goes to the cloud. |
| **Test questions** | Questions you write, with the facts each answer must contain, to check a model before you need it. |

---

## Part 1: What RouterLM does

Most people using AI at work already have the pieces: an account with a model company, an API key, maybe
LM Studio on a laptop. Each app talks straight to one model. When that model stops answering, the app
stops too. The company has an outage, the card on the account expires, the internet goes down.

RouterLM sits between your apps and your models. Every app sends its questions to RouterLM, and RouterLM
hands each one to the first model in the lineup that can answer. When one can't, it moves to the next.
It runs on a Mac or a Debian/Ubuntu machine in the office, or on the Mac on your desk.

```
  your apps                 RouterLM                    your models, in order
  ─────────                 ────────                    ─────────────────────
  Capsule      ──┐                                  ┌─▶ #1 Claude        (cloud)  Primary
  a script     ──┼──▶  one address, one lineup  ────┼─▶ #2 GPT           (cloud)
  chat window  ──┘                                  └─▶ #3 Qwen 27B      (office Mac)
```

### 1. Keep working

The lineup is the fallback. You set the order, so you know what happens when #1 can't answer, and the main
page shows each lineup's order if you forget. Some ways to set one up:

- **Cloud to cloud.** Claude first, GPT second. Claude has an outage and GPT answers. Most days nobody
  notices.
- **Cloud to local.** Claude first, a model on the office Mac second. The internet goes down and the
  office Mac answers. Slower and less capable, and the work keeps moving.
- **Local to cloud.** The office Mac first, so most work stays in the building; Claude behind it for when
  the Mac is busy or off.

A model "can't answer" when the company refuses the API key, the account is out of credit, the model is
overloaded, the first word takes too long, or the local machine is off. In every case RouterLM moves to
the next model. A problem that needs a person also puts an alert on its page: an API key refused or missing,
an account out of credit, a changed model file, a different model loaded. So the expired card shows up as an
alert and not as a mystery. An outage or a slow answer doesn't make an alert; it shows in the model's dot and
in recent questions.

### 2. Specialists

Different work, different lineups. A small local model is good at summaries and tagging. A large cloud
model is better at hard questions. So you might keep:

- **summaries**: a small local model first
- **answers**: Claude first, the office Mac last
- **hr**: Local only (below)

An app asks for a lineup by name. Most people start with one lineup and add a second when they need it.

### 3. Keep work in the building

Mark a lineup Local only and it uses only the models on your own machines, its backups included. Mark an
app Local only and every question it sends stays in the building, whichever lineup it asks for, so an HR
app can't reach a cloud model even by mistake.

Moving a whole office from cloud to local is a change in one place: reorder the lineup, and every app
follows.

Search indexing (embeddings) always uses one local model and never falls back. Switching models halfway
would mix two indexes that can't be compared.

### 4. One address for every app

Point each app at RouterLM once. After that you change models in RouterLM, not in each app. No app is tied
to one company, and trying a new model is a change to a lineup.

### 5. Know your backup is good before you need it

A backup that makes things up is worse than no backup. RouterLM keeps the test questions you write, with
the facts each answer must contain and the claims it must not make. Run them against any model: it
counts right answers, honest "I don't know"s, and wrong ones. A backup that states something wrong
doesn't pass. You find this out on a quiet afternoon instead of during an outage.

### 6. See what's going on

One page shows each lineup in order, which model answered each question, when RouterLM fell back and why,
the alerts for problems a person has to fix (an API key refused or missing, an account out of credit, a
changed model file), and how many questions each app asked per day. It records that a question was asked,
by which app, and which model answered. It doesn't keep the question or the answer unless you switch on
Troubleshooting, and then the page says so in red.

### 7. One local machine, shared

A local machine answers a few questions at a time. RouterLM lets a person who's waiting go ahead of a
background job, and when every model is busy it tells the app to try again shortly instead of leaving it
hanging.

### 8. A local model that stays the same

A local model is a file. Pin it, and if the file changes, RouterLM sets that model aside and the next model
in the lineup answers in its place. The alert says which file changed. Run the test questions against the
new file; if it passes, update the pin and the model is back at its spot in the lineup.

---

## Part 2: Install on macOS

Three steps, from nothing to your app's first answer. Terminal comes up once, for one pasted line; everything
after that happens in a browser.

You need a Mac and at least one of these: a local AI app running a model (LM Studio, Ollama or llama-server), or
an API key from Anthropic or OpenAI.

### 1. Install RouterLM

Open Terminal: press Cmd-Space, type Terminal, press Return. Paste this line and press Return:

```bash
curl -fsSL https://raw.githubusercontent.com/rene-kropf/routerlm/main/install.sh | sh
```

It puts RouterLM in a folder named `routerlm-main` in your home folder, the one with your name on it where
Documents and Downloads are. RouterLM keeps its settings and API keys in that folder. The line also sets
RouterLM to start whenever this Mac starts and to start again if it ever stops, then opens it in your browser.
You can close Terminal now. macOS may show a "Background Items Added" notice: that's RouterLM starting with the
Mac.

RouterLM is written in Python. If this Mac has never run it, the line says so and a window offers to install the
command line developer tools, which include Python. Click Install, wait for it to finish, and paste the line
again.

The page is http://127.0.0.1:8090/. 127.0.0.1 is the address a computer uses for itself, so it opens only on the
Mac running RouterLM, and there it doesn't ask for a password. With nothing set up yet, it opens Settings with
Quick start at the top.

To update RouterLM, paste the same line again; your settings and API keys stay. To remove it, paste this line,
then put the `routerlm-main` folder in the Trash if you want its settings and API keys gone too:

```bash
launchctl bootout gui/$(id -u)/io.github.rene-kropf.routerlm; rm ~/Library/LaunchAgents/io.github.rene-kropf.routerlm.plist
```

On Linux (Debian or Ubuntu), the line doesn't install anything. Download RouterLM from
github.com/rene-kropf/routerlm (the green "Code" button, then "Download ZIP"), unzip it into your home folder,
and start it with `cd routerlm-main && python3 routerlm.py serve`. It runs while that terminal stays open.

### 2. Quick start

Quick start sets up an `answer` lineup for your apps in one step. What you do depends on what you have:

- **A local AI app running a model.** Pick the model under "Model on this machine" and press "Use it". If the
  box says "No local AI app answered on this machine", start the local AI app and reload the page. If LM Studio
  or Ollama is installed but not running, the Models box lower down has a button that starts it.
- **An API key as well.** Add it first, under API keys: a name, the key, "Add API key". Quick start then also
  shows "Cloud model as Primary". Pick a cloud model there to be #1; the local model becomes #2.
- **Only an API key.** Add it under API keys. In its row, under "Used by", pick a model and press "Use it".

A green box at the top of the page says what to put in your app:

> Done. In your app, put http://127.0.0.1:8090/v1 as the address and answer as the model. On this Mac the app
> needs no API key: put its name in the API key box, and that's how it shows on the main page. The answer
> lineup: #1 Primary google/gemma-4-31b (LM Studio, local).

### 3. Point your app at RouterLM

RouterLM takes questions the way OpenAI does, so an app that can use OpenAI can use RouterLM. In the app's
settings, where it asks for OpenAI's address (often called the base URL), the model and the API key, put:

| Setting | What to put |
|---|---|
| Address (base URL) | `http://127.0.0.1:8090/v1` |
| Model | `answer` |
| API key | the app's name, such as `capsule`, or nothing |

The API key box only names the app: the main page lists the app's questions under that name, and with the box
left blank it shows as "this Mac". An OpenAI key left in the box works too, and shows as "this Mac"; RouterLM
doesn't use it.

If the app sends its questions to a full web address, as a script or a tutorial's code often does
(`https://api.openai.com/v1/chat/completions`), change it to `http://127.0.0.1:8090/v1/chat/completions`. An app
that only talks to Anthropic's own API needs its OpenAI-style settings instead; most apps and tutorials offer
both. The app has to run on this same Mac; for apps on other computers, see Apps and General in Part 3.

Ask your app something. The first question to a local model can take a minute while the local AI app loads the
model. On RouterLM's main page ("← Main page" at the top of Settings), the question shows under Recent questions
with the model that answered it.

---

## Part 3: A tour of the screens

RouterLM has two pages. Settings is where you set it up: the gear at the top right of the main page. The main page,
at http://127.0.0.1:8090/, is where you see what's happening.

Every change in Settings is checked before it's saved. If something is wrong, a red box says "Not saved." with
the reasons, and nothing changes. A saved change works at once; only a new network address needs a restart.

### Settings

The page is headed "RouterLM settings", with "← Main page" to go back. Quick start sits at the top until the
first lineup exists (Part 2).

#### API keys

The API keys from your accounts with Anthropic, OpenAI and other cloud model companies. Add one with a name,
the key and "Add API key". Any name works; `anthropic` is filled in. After you save a key, the page shows only
its last 4 characters, under "Ends with". The key stays on this Mac, in a file only RouterLM's user account can
open.

"Used by" lists the models that use the key. Until one does, it offers that company's models: pick one and
press "Use it", and it becomes #1, the Primary, in the `answer` lineup. If the company refuses the key, "Used
by" says so in red. To move a model to another key, pick the key on the model's card under Models.

"Replace" swaps in a new key from the same account; "Delete" removes it. Two accounts with one company can back
each other up: add both keys and put the model in a lineup once with each. The lists show which key each one
uses.

#### Models

The models you can put in a lineup. At the top are the local AI apps on this Mac: LM Studio, Ollama and
llama-server. Each is "running" (with how many models), "installed, not running" (with a button to start it,
such as "Start LM Studio"), or "not found on this machine".

You seldom add a model here. The lists under Lineups offer every model your API keys and local AI apps have, and
picking one there adds it. Each model in use gets a card headed by its name and where it runs, such as
"claude-opus-5-5 (Anthropic, cloud)". On the card:

- "Model name, as the company or local AI app lists it", "Web address", "Where it runs" and "Type" were filled
  in when the model was added. Leave them.
- "API key": which of your API keys this model uses.
- "Questions it works on at once (blank: 2 local, 8 cloud)": a local machine answers a few questions at a time.
  The rest wait their turn, or go to the next model in the lineup.
- "File fingerprint (optional, local models)" locks a local model to one exact file (Part 1, section 8). A
  fingerprint is a long code worked out from the file's contents: change one byte of the file and the code
  changes. A test-questions run on the main page shows the fingerprint to paste here.
- "Model file location (only with a fingerprint, if the local AI app doesn't say)": where the model's file is.
  llama-server says; LM Studio and Ollama don't, so fill it in for them before a test-questions run.

"Add a model" is for a model the lists don't offer: a cloud company with an OpenAI-compatible API, or a local
AI app on another machine in the office. Pick one of the two "give its address" choices under "Where it runs",
then fill in "Model name" and "Web address", plus "API key (cloud)" for a cloud company, and press "Add model".
A model that's already set up is turned away with "is already set up"; the same model with a second API key gets
a card of its own. "Delete model" removes a model.

#### Lineups

Each lineup has a card with its light (the main page explains the colors) and its name. The slots run
"#1 Primary", "#2", "#3", with one empty slot at the end for the next model. Pick a model in a slot and press
Save. To change the order, pick different models in the slots; to drop one, set its slot to "—".

- "Priority": "A person is waiting: goes first", or "Background work: waits until no one is waiting". Pick
  background for a lineup that does bulk work, such as tagging overnight, so people asking questions go first.
- "Which models": "Cloud and local", or "Local only: nothing goes to the cloud". A Local only lineup skips its
  cloud models, backups included.
- "Move on if no answer starts within (seconds)": how long RouterLM waits for a model to start answering before
  it asks the next one. Blank is 30. Quick start sets 60 when a cloud model is #1 and 120 when a local model is,
  because a local model can take a while to load.
- "Longest question, in tokens of about 4 characters (blank: no limit)": a longer question is turned away, never
  cut short, and the app is told it was too long.

"Add a lineup" takes a name and its "#1 Primary"; add #2 and the rest on its card. Names use lowercase letters,
digits, dots, dashes and underscores. An app asks for a lineup by this name, in its model setting. A lineup named
`embed` with one local model is for search indexing (embeddings); it never moves on to another model. "Delete
lineup" removes a lineup; an app asking for it gets "no lineup by that name".

#### Apps

The box says what goes in an app's settings: "put http://127.0.0.1:8090/v1 as the address and a lineup's name
as the model."

An app on this Mac needs no API key and doesn't have to be added here. What it puts in its API key box is its
name on the main page; left blank, it shows as "this Mac". With no apps added, the list says "None added. Apps
on this Mac work without being added."

Add an app here for one of two reasons:

- **To give an app on this Mac settings of its own.** Add it under the name it puts in its API key box, and the
  settings below apply to it.
- **For an app on another computer.** It needs an API key. "Add app and make its API key" makes one, and the
  page says "API key for" the app's name, then "copy it now. RouterLM can't show it again." Copy the key into
  the app. The other computer also needs RouterLM on an address it can reach (General).

On each app added here:

- "Which models" set to "Local only: nothing goes to the cloud" keeps everything the app sends in the building,
  whichever lineup it asks for.
- "May ask for a model by name, not only a lineup": the app can put a model's own name, such as `gpt-5.5`, as
  the model. That model answers alone, with no backup.
- "New API key" makes a new key for the app; the old one stops working at once. "Delete" removes the app; its
  API key no longer works from other computers.

#### General

- "RouterLM's address on the network (a change needs a restart)": `127.0.0.1:8090` means only this Mac can reach
  RouterLM. For apps or browsers on other computers, put this Mac's address on the office network with the
  port, such as `192.168.1.20:8090`. System Settings, Network, Details shows the Mac's address. Save, then press
  "Restart RouterLM". From then on RouterLM is at the new address, on this Mac too.
- "After a model fails, skip it for (seconds)": blank is 60. While a model is skipped, its lineups go straight
  to the next model. If every model in a lineup is being skipped when a question comes in, RouterLM tries
  them anyway, in order, except a model whose API key is missing.
- "Check local AI apps every (seconds)": blank is 5. How often RouterLM asks each local AI app whether it's up,
  so a machine that's off is skipped without waiting.
- "Troubleshooting: also save every question and answer in full, in a separate file". RouterLM normally records
  that a question was asked, by which app, and which model answered, and not the question or the answer. Tick
  this to keep both while you track down a problem. The main page shows a red banner while it's on.

#### Opening this page from other computers

On the Mac running RouterLM, the pages never ask for a password. Other computers can't open them until a
password is set here: "New password", "Same again" (8 characters or more), "Set password". Then a browser on
another computer asks for a name and password; any name works. Other computers also need RouterLM on an address
they can reach (General). The password is for these pages only; apps on other computers use their API keys.

#### The settings file

"Everything above as text", for whoever would rather edit the file itself. API keys aren't in it. "Save file"
goes through the same checks as the boxes above. If you set RouterLM up from the boxes, you can leave this
alone.

"Export" downloads this file, to keep a copy or to set up another RouterLM the same way. "Import" loads a file
you pick and saves it through the same checks. API keys never go with it: after an import, a model whose API key
isn't on this Mac says "API key missing" until you add the key under API keys.

### The main page

The top line shows RouterLM's version, this Mac's name and RouterLM's address, with the Settings gear at the
right.
Under it: how long RouterLM has been running, how long the Mac has been on, memory in use and disk free. The page
refreshes itself every 15 seconds.

#### Alerts

Problems a person has to fix: "Model", "Problem", "Detail", "First seen", "Last seen", "Times". With none it
says "None. Everything is answering." The problems:

- "API key rejected": the company refused the key. Replace it under API keys.
- "account out of credit": the account needs paying, or the card on it expired.
- "API key missing": the model's key isn't saved. Add it under API keys and pick it on the model's card.
- "local AI app reports a different model": the local AI app has another model loaded than the one set up.
- "model file changed": a locked model's file isn't the one it was locked to (Part 1, section 8).
- "can't check the model file": RouterLM can't find or read a locked model's file.

While a model has an alert, its lineups use the next model. The alert clears itself when the model answers
again. An outage or a slow answer doesn't make an alert: RouterLM moves on, tries the model again after the wait
set in General, and it shows in the model's dot under Lineups and in Recent questions.

#### Lineups

Every lineup, with "Light", "Lineup", "Last answered" and "Models, in order", #1 marked Primary. The lineup's
light: green, the Primary is answering; yellow, a backup is answering or answers are slow; red, nothing in the
lineup can answer.

Each model in the row has a dot of its own, and a word on how it's doing:

- Green: its last try answered ("answered" and the time), or it's a local model nobody has asked yet and its
  local AI app is ready ("ready, not asked yet").
- Red: it's down now ("down" and the reason), or its last try failed ("failed", the time and the reason). A
  model that's down is skipped until the wait set in General is over. Its dot stays red after that, until the
  model answers again.
- Grey: it hasn't been asked yet ("not asked yet").

To change the order, go to Settings.

#### Try it

Ask a lineup a question from the page, the way an app would: pick the "Lineup", type a "Question", press
"Send". Tick "Local only: local models answer, nothing goes to the cloud" to keep that one question in the
building. The answer shows with the model that gave it and the time it took, such as "Answered by gpt-5.5
(OpenAI, cloud) · the answer started after 0.8 s, done after 2.1 s", plus "moved on from" and the model that
couldn't answer, when one couldn't. Try it questions show under Recent questions as the app `try-it`.

#### Recent questions

The last 25 questions, newest first: "Time", "App", "Lineup", "Answered by", "Result", "Answer started after",
"Done after", "Moved on from". "App" is the app's name, or "this Mac" for an app on this Mac that sent none.
"Moved on from" names the model that couldn't answer before this one did. A result other than "answered" shows
in red, such as "no model could answer", "every model busy; the app was told to retry", "the question was too
long", "no lineup by that name" or "the app stopped waiting". The questions and answers themselves aren't here:
RouterLM doesn't keep them unless Troubleshooting is on.

#### Questions and tokens per day

The last 7 days, per app: "Questions", "Tokens in", "Tokens out". Tokens are how model companies count and bill
text, about 4 characters each. In is the question; out is the answer.

#### Test questions

Whether a model is good enough before you rely on it (Part 1, section 5). Until someone writes them, it says
"None yet" and where the file goes; whoever set RouterLM up writes them.

Once they're there, a line says how many questions there are, whether they may go to the cloud, who grades the
answers, and how many times a backup may say "I don't know"; "the Primary has to get every one right". Each
model gets a row: "Last run", "Right / don't know / wrong", "Graded by", "Good enough?", "Model file", and a
"Run test questions" button. "Good enough?" answers "passes" or "fails" for each lineup the model is in, as the
Primary or as a backup. A run asks every question of that one model, one at a time, with no backup, and the row
counts up as it goes. When a model grades the answers, the row says "not checked by a person yet" until someone
has. Checking the grades is the one job here that needs a terminal (`python3 routerlm.py grade`), for whoever set
RouterLM up.

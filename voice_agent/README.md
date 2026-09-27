# Voice agent

Talk out loud with **Kabir**, an AI agent running on [Groq](https://groq.com), and hear him answer. It is hands-free: press one button, then just talk. It runs on your own computer, as a small local server and a page in your browser. It needs nothing beyond this project's existing install.

It is separate from the Instagram outreach system and doesn't touch it.

## 1. Put your Groq key in `.env`

Create a key at [console.groq.com/keys](https://console.groq.com/keys). Then, in Command Prompt:

```bat
cd C:\Users\Rohit\insta-outreach
notepad .env
```

Add this line with your key, then save and close Notepad:

```ini
GROQ_API_KEY=gsk_your_key_here
```

The key stays on your computer. The page never sees it, and neither the page nor the console ever shows it.

Optional lines for the same file:

| Line | What it does |
|---|---|
| `GROQ_MODEL=llama-3.3-70b-versatile` | Use this chat model. By default he picks the best one your key can use, from Groq's live list, each time he starts. |
| `GROQ_STT_MODEL=whisper-large-v3` | The speech-to-text model. Default: `whisper-large-v3-turbo`. |
| `VOICE_AGENT_NAME=Arjun` | His name. Default: Kabir. |
| `VOICE_AGENT_PROMPT=You are {name}, ...` | Replace his personality. Keep it on one line; write `\n` for a line break. `{name}` becomes his name. |

## 2. Start him

In the same Command Prompt window:

```bat
.venv\Scripts\activate
python -m voice_agent
```

You'll see something like this:

```text
Starting Kabir...
Model: openai/gpt-oss-120b (speech to text: whisper-large-v3-turbo)
Talk to Kabir at http://127.0.0.1:8770  (press Ctrl+C to stop)
```

The page opens in your browser. To stop him, press **Ctrl+C** in that window.

- `python -m voice_agent --port 8771` uses another port.
- `python -m voice_agent --no-browser` doesn't open the browser.

## 3. Use Microsoft Edge and allow the microphone

**Microsoft Edge** gives him a natural voice. On Windows, Edge includes free "Online (Natural)" voices: he uses *Prabhat* (Indian English) and *Madhur* (Hindi). Chrome works too, but its voices sound robotic.

If Edge is not your default browser, open the page in Edge yourself:

```bat
start msedge http://127.0.0.1:8770
```

When the browser asks to use the microphone, click **Allow**.

## 4. Talking to him

Press **Start talking**. He says hello, then listens. Talk normally and **pause when you're done**. About ¾ of a second of silence ends your turn. He answers out loud, and the conversation appears on the right. He speaks English, Hindi or Hinglish, matching you.

The face shows what he is doing:

| Colour | Meaning |
|---|---|
| Blue | Listening. |
| Green | Hearing you. |
| Orange | Thinking. |
| Pink | Talking. |

| Control | What it does |
|---|---|
| **Start talking / Stop** | Turns the microphone on or off. |
| **Interrupt** or the **Space** key | Stops him mid-answer so you can talk. |
| **Mute mic** | Stops listening, for a phone call for example. |
| **New conversation** | Forgets everything said so far. He remembers the last 20 messages. |
| **Voice and settings → Voice** | Choose his voice. The browser remembers your choice. |
| **Voice and settings → Allow interrupting by voice** | Talk over him to interrupt. **Use headphones**, or he will hear himself. |

While he talks, he doesn't listen, so his own voice from the speakers can't start a new turn. While he is still thinking, he does keep listening. If you add something after a pause, he waits and answers everything together.

## 5. Troubleshooting

| What you see | What to do |
|---|---|
| **"Groq key needed"** on the page, or **"Add GROQ_API_KEY=..."** in the console | Do step 1, then run `python -m voice_agent` again. The open page picks it up by itself. |
| **"Groq rejected the key"** | The key is wrong or was deleted. Create a new one, put it in `.env`, and start him again. |
| **"Microphone blocked"** | Click the lock icon left of the address, set *Microphone* to *Allow*, and press Start talking. Also check Windows **Settings → Privacy & security → Microphone**: *Microphone access* and *Let desktop apps access your microphone* must be on. |
| He doesn't react when you talk | Check the pill at the top right says *Mic on* and not *Mic muted*. In Windows sound settings, check the right microphone is the default input. Move closer to the mic. |
| He reacts to the TV or other people | A quieter room or a headset helps. |
| No voice, or a robotic one | Use Microsoft Edge (step 3). Check the Windows volume. Try another voice under *Voice and settings*. His replies are always shown as text too. |
| **"Groq is busy, try again in N s"** | You hit Groq's rate limit (the free tier has one). Wait that long. |
| **"Can't reach Groq"** | Check your internet connection. |
| **"Port 8770 is busy"** | He is probably already running in another window. Or use `--port 8771`. |
| `No module named ...` | Activate the project's environment first: `.venv\Scripts\activate`. The main [README](../README.md) shows how to set it up. |
| He answers his own words | Turn off *Allow interrupting by voice*, or use headphones. |

## How it works

The page records your turn and sends it to the local server. The server sends it to Groq's Whisper for a transcript, then streams the reply from a Groq chat model back to the page. The page speaks each sentence as soon as it arrives, so he starts talking before the whole answer is written.

Only this computer can use the server: it listens on `127.0.0.1` and refuses requests from other websites. Your audio and the conversation go to Groq to be transcribed and answered. The voice agent itself saves nothing: the conversation lives only in the open page. The browser remembers just your voice choice and the interrupt-by-voice setting.

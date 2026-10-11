# Instagram Daily Auto-Post: Setup Guide

Roj 2 posts: 1) AI / tech news, 2) innovative tech idea.
AI (free Gemini) content banavto, Python script image banavte ani Instagram var post karte, UiPath roj trigger karto.

```
UiPath Orchestrator (time trigger)
        |
        v
  run_news.bat / run_idea.bat
        |
        v
  main.py:  RSS news -> Gemini (free) -> image (1080x1350) -> Cloudinary -> Instagram API
        |
        v
  log.csv  (har post cha result)   +   exit code != 0 -> UiPath sends alert email
```

Ek vela setup (~45 minute). Tyanantar kahi karaych nahi (fakt token refresh automatic).

---

## PART A: Ek vela setup (tumhala swatah karaych)

### A1. Python install
1. https://www.python.org/downloads/ var Python 3.11+ install kara. Install screen var **"Add python.exe to PATH"** tick kara.
2. Hi folder (`insta_automation`) PC var `C:\insta_automation` madhe theva.
3. Command Prompt ughda ani:
   ```
   cd C:\insta_automation
   pip install -r requirements.txt
   ```

### A2. Free AI key (Google Gemini, kharch 0)
1. https://aistudio.google.com/apikey var Google account ne login kara.
2. **Create API key** -> copy kara. Card / payment dyayla lagat nahi (free tier).
3. `.env.example` copy karun `.env` naav kara, `GEMINI_API_KEY=` jaage var paste kara. `LLM_PROVIDER=gemini` tasach theva.
4. Jar kadhi "model not found" error aala, tar AI Studio madhe current Flash model cha naav pahun `GEMINI_MODEL` badla.

(Free tier chya limits Google badlu shakto. Roj 2 posts sathi khup kami vapar ahe, tyamule free madhe basto. Claude vaparaychach asel tar `LLM_PROVIDER=claude` ani Claude API key lagel, pan te paid ahe.)

### A3. Instagram account tayar kara
1. Instagram app -> Settings -> **Account type and tools** -> **Switch to professional account** -> Creator kiwa Business.
2. Ek Facebook Page lagto: Business account sathi page var link kara. (Creator sathi pan kahi vela lagto.)

### A4. Meta Developer App ani token
1. https://developers.facebook.com var login kara -> **My Apps -> Create App** -> use case: **Other** -> type: **Business**.
2. App dashboard madhe **Add Product -> Instagram** (kiwa "Instagram API with Instagram login") select kara.
3. **API setup with Instagram login** page var:
   - **Add account** -> tumcha Instagram professional account connect kara (login karun permissions allow kara).
   - **Generate token** button daba, token copy kara. Tyach var tumcha **Instagram user ID** disto.
   - Permission madhe `instagram_business_basic` ani `instagram_business_content_publish` ahet he check kara.
4. `.env` madhe `IG_ACCESS_TOKEN=` ani `IG_USER_ID=` bhara. `IG_HANDLE=@tumcha_handle` pan lihaa.

Tip: App "Development mode" madhe tumcha swatacha account tester mhanun asel tar app review lagat nahi. Fakt swatachya account var post karnar asel tar tasech theva.

### A5. Cloudinary (image hosting)
Instagram la image cha public URL lagto, mhanun free Cloudinary vapartoy.
1. https://cloudinary.com var free account kara. Dashboard var **Cloud name** disto.
2. Settings -> **Upload -> Upload presets -> Add upload preset** -> **Signing mode: Unsigned** -> Save.
3. `.env` madhe `CLOUDINARY_CLOUD_NAME` ani `CLOUDINARY_UPLOAD_PRESET` bhara.

### A6. Test (post na karta)
```
cd C:\insta_automation
python main.py --slot news --dry-run
python main.py --slot idea --dry-run
```
`output` folder madhe image + caption disel. Changle vatle tar pudhe jaa.

### A7. Pahila real post (test)
```
python main.py --slot news
```
Instagram var post aala ka te pahaa. `log.csv` madhe `OK` disel.

---

## PART B: UiPath madhe automation

UiPath cha Robot tumchya PC var chalato, mhanun **PC roj tya velela chalu aani internet var asla pahije**. (PC band asel tar post jaat nahi. 24x7 sathi Windows VM / always-on PC vapra.)

### B1. 3 processes banva (UiPath Studio ghe kiva Studio Web)
Prattek process madhe fakt 3 activities:

**Process 1: `Insta_News`**
1. **Start Process** activity
   - FileName: `C:\insta_automation\run_news.bat`
   - WorkingDirectory: `C:\insta_automation`
   - Output ExitCode -> variable `exitCode` (Int32), StandardError -> `errText` (String)
2. **If** `exitCode <> 0` -> **Send SMTP Mail Message** (kiwa Gmail activity):
   - To: tumcha email
   - Subject: `Instagram news post FAILED`
   - Body: `errText`
   - Tyanantar **Throw** activity (Orchestrator madhe job Faulted dista).

**Process 2: `Insta_Idea`**: same, fakt FileName `run_idea.bat`, subject `idea post FAILED`.

**Process 3: `Insta_RefreshToken`**: same, FileName `run_refresh_token.bat`.

Tyanantar **Publish** kara (Orchestrator var).

### B2. Orchestrator madhe Triggers (Time triggers)
Orchestrator -> tumcha folder -> **Triggers -> Add Trigger -> Time**:

| Process | Cron / Schedule | Vel |
|---|---|---|
| Insta_News | `0 0 9 * * ?` | roj sakali 9:00 |
| Insta_Idea | `0 0 18 * * ?` | roj sandhyakali 6:00 |
| Insta_RefreshToken | `0 0 3 ? * SUN` | roj aathvdyala ravivaari raatri 3:00 |

Timezone: **(UTC+05:30) India Standard Time** nivda.

Robot: Unattended robot (kiva Attended with auto-login) tumchya machine var assign kara. Windows lock zala tar unattended robot la "Login to console" setting lagte.

### B3. Pahila full test
Orchestrator madhe process manually **Start Job** kara. Job Successful asel ani Instagram var post asel tar sagla ready.

---

## Roj kay hote (tumhala kahi karaych nahi)
- Sakali 9: AI news post. Sandhyakali 6: tech idea post.
- Navin batmya 7 sources (TechCrunch, The Verge, VentureBeat, Google AI, OpenAI, Anthropic, Ars Technica) madhun yetat. Jya batmya aadhi post zalya tya parat yet nahit (`posted.json`).
- Caption madhe source naav + "(AI-assisted post)" auto yeto.
- Kahi fail zale tar UiPath tumhala email pathavto, ani `log.csv` madhe karan disto.

## Customize karaychay?
| Kay | Kuthe |
|---|---|
| Language (Marathi / Hinglish) | `.env` -> `POST_LANGUAGE`. Marathi sathi Noto Sans Devanagari Bold font download karun `FONT_PATH` madhe path dya (nahi tar image var ?? disel) |
| Tone / style | `main.py` -> `build_prompt()` |
| News sources | `main.py` -> `FEEDS` list |
| Image colors / layout | `main.py` -> `make_image()` |
| Posting vel | Orchestrator Triggers |

## Mahatvachya gosti
1. **Accuracy:** Claude fakt RSS madhun milalelya batmya vaparto, tarihi mothya brand cha post asel tar mahinyatun ekda `output` folder madhe caption tapasa.
2. **Token:** Instagram token ~60 divsa tikto. `Insta_RefreshToken` process te auto renew karto. Te band thevu naka. Token expire zala tar posts band hotil ani email yeil (tevha A4 parat kara).
3. **Cost (sagla free):** Gemini free tier, Instagram API, Cloudinary free plan, Python, UiPath Community Edition. Fakt tumcha PC ani internet lagto. UiPath Community chya free limits (unattended robot) tumchya account madhe tapasa, kahi vela te badlatat. Jar UiPath madhe automatic run free nasel, tar Windows **Task Scheduler** (free, Windows madhe ata ahe) madhe `run_news.bat` / `run_idea.bat` roj 9:00 ani 18:00 la schedule kara, tyane UiPath chi garaj padat nahi.
4. **Instagram rules:** Roj 2 posts limit madhe ahet (limit 100/24 tas). Spammy hashtags vaparu naka.
5. **Security:** `.env` kunalahi share karu naka, email/WhatsApp/GitHub var takau naka.

## Problems
| Error | Upay |
|---|---|
| `Invalid OAuth access token` | Token expire / chukicha. A4 parat kara. |
| `create container failed ... image_url` | Cloudinary preset **Unsigned** ahe ka tapasa. |
| `no fresh items found` | Internet / RSS block. Thoda vel nantar parat chalel. |
| `Gemini API error 404` / model not found | AI Studio madhe current model cha naav `GEMINI_MODEL` madhe takaa. |
| `Gemini API error 429` | Free limit sampli. Kahi vel thamba, roj 2 posts sathi sahasa yet nahi. |
| `KeyError: GEMINI_API_KEY` | `.env` madhe key bharli nahi / `.env` chukichya folder madhe ahe. |
| Marathi image var `????` | `FONT_PATH` madhe Devanagari font cha path dya. |

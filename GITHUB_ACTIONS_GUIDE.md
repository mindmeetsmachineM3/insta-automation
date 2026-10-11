# GitHub Actions Setup (PC band asla tari chalel, kharch 0)

Ya version madhe UiPath / tumcha PC lagat nahi. GitHub chya free servers var roj automatic chalte:
- 9:00 AM IST: AI news post
- 6:00 PM IST: tech idea post
- Ravivaar 3:00 AM IST: Instagram token auto-refresh

Aadhi `SETUP_GUIDE.md` madhale **A2 (Gemini key), A3, A4 (Instagram), A5 (Cloudinary)** pahile kara. Fakt `.env` madhe nahi, tya values GitHub Secrets madhe jatil.

---

## Step 1: Private repo banva
1. https://github.com var free account kara / login kara.
2. **New repository** -> naav `insta-automation` -> **Private** nivda (mahatvache: Private) -> Create.
3. Zip mhadhil sagle files upload kara: **Add file -> Upload files**. `.github` folder pan jaaila pahije (hidden folder ahe, Windows var "Show hidden items" on kara, kiva Git vapra).
   - `.env` file upload karu naka (ti banvli asel tar).

Git vaparayche asel tar:
```
cd C:\insta_automation
git init
git add .
git commit -m "first"
git branch -M main
git remote add origin https://github.com/TUMCHE_NAAV/insta-automation.git
git push -u origin main
```

## Step 2: Secrets bhara
Repo -> **Settings -> Secrets and variables -> Actions -> New repository secret**. 6 secrets:

| Secret naav | Value |
|---|---|
| `GEMINI_API_KEY` | Gemini free key |
| `IG_USER_ID` | Instagram user ID |
| `IG_ACCESS_TOKEN` | Instagram token |
| `CLOUDINARY_CLOUD_NAME` | Cloudinary cloud name |
| `CLOUDINARY_UPLOAD_PRESET` | Cloudinary unsigned preset |
| `GH_PAT` | Step 3 madhla token |

**Variables** tab madhe (optional):
- `IG_HANDLE` = `@tumcha_handle`
- `POST_LANGUAGE` = `English` / `Marathi` / `Hinglish`
- `GEMINI_MODEL` = jar default model chalat nasel tar

## Step 3: GH_PAT (token auto-refresh sathi)
Instagram token roj navin hoto, to GitHub secret madhe automatic save karnyasathi ek token lagto.
1. GitHub -> profile photo -> **Settings -> Developer settings -> Personal access tokens -> Fine-grained tokens -> Generate new token**.
2. Repository access: **Only select repositories** -> `insta-automation`.
3. Permissions -> Repository permissions -> **Secrets: Read and write**.
4. Expiration: sarvat jast (1 year). Expire zalyavar navin banvun secret update kara.
5. Token copy karun `GH_PAT` secret madhe paste kara.

## Step 4: Pahila test (Instagram var post na karta)
1. Repo -> **Actions** tab -> "Instagram daily posts" -> **Run workflow**.
2. action = `news`, **dry_run = tick (yes)** -> Run.
3. Run complete zalyavar tyat **preview** artifact download kara: image + caption disel.

## Step 5: Real test
Parat Run workflow, ata **dry_run = untick**. Instagram var post aali ka pahaa.
Tyanantar kahich karaych nahi: schedule automatic chalel.

---

## Mahatvache
- **Repo Private theva.** Public asel tar 60 divsa activity nasel tar schedule band hote, ani posted/log files sarvana distat.
- **Free limits:** Private repo la sadharan 2000 free minutes/mahina miltat (GitHub kadhi badlu shakto, Settings -> Billing madhe tapasa). Ha workflow roj ~2 minute vaparto, mhanje ~60-70 min/mahina.
- **Timing:** GitHub schedule kadhi kadhi 5 te 30+ minute late chalu shakto (jaast gardi chya veli). Tarihi roj hoil.
- **Fail zale tar:** GitHub tumhala email pathavto ("workflow failed"). Actions tab madhe log baghun karan disel. Notifications on asavet: GitHub -> Settings -> Notifications -> Actions.
- **Token:** `Refresh Instagram token` job fail zali (GH_PAT chukicha/expire) tar 60 divsa nantar posts band hotil. Email alert yeil.
- Time badlayacha asel tar `.github/workflows/instagram.yml` madhe `cron` ani `case` madhe dono thikani same badla (UTC madhe: IST minus 5:30).

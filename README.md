# pcdl：社群平台一鍵下載

複製貼文連結、按一下 iPhone 捷徑，就能把 Plus Chat、X、Instagram、Threads 貼文裡的照片和影片存進相簿。

手機上只留一個捷徑，不用另外安裝 App。解析貼文、合併影片都交給自架的雲端伺服器（Flask + Render），捷徑只負責傳連結和存檔。

## 功能

- **多平台解析**：Plus Chat、X、Instagram、Threads（TikTok、抖音已實作但未實測）
- **串流影片合併**：Plus Chat 的影片是 HLS／DASH 串流片段，伺服器下載全部片段後用 ffmpeg 合併成 mp4；只換容器、不重新編碼，畫質不損失，並優先使用位元率較高的 DASH
- **選擇性下載**：伺服器回傳選單（全部下載、影片 1、照片 2…），在捷徑裡勾選要的項目
- **流量煞車**：串接 Render 流量 API，本月用量達上限（預設 4.5 GB，免費額度 5 GB）就暫停影片下載，避免超額扣款；照片不經過伺服器，不受影響
- **診斷頁面**：`/usage` 查本月流量、`/cookies` 檢查登入狀態，出錯時直接看到原因
- **多組金鑰**：可同時發給朋友使用，刪掉某一組就能單獨停用

## 下載流程

```
iPhone 捷徑                     Render 伺服器（Flask）
──────────                     ─────────────────────
複製連結、執行捷徑
   │  key + url
   ├──────────────────────────▶ /list   解析貼文，回傳選單
   │◀────────────────────────── 全部下載／影片1／照片2…
選擇項目
   ├──────────────────────────▶ /urls   回傳每個項目的下載網址
   │◀──────────────────────────
逐一下載
   ├─ 照片、X／Threads 影片：手機直接下載（不吃伺服器流量）
   └─ Plus Chat／IG 影片、IG／Threads 照片：/item 由伺服器下載後轉傳
存進相簿
```

## 各平台做法

| 平台 | 解析方式 | 檔案怎麼到手機 |
|---|---|---|
| Plus Chat | 讀網頁內的貼文資料；影片用 ffmpeg 合併 DASH／HLS 片段 | 照片直接下載；影片經伺服器 |
| X | gallery-dl（不需登入），失敗時改用 yt-dlp | 手機直接下載 |
| Instagram | gallery-dl + 小號 cookies | 經伺服器轉傳 |
| Threads | 自己寫的解析器 + 小號 cookies；支援 App 分享的 `/share/` 短網址 | 照片經伺服器轉傳；影片直接下載 |
| TikTok、抖音 | yt-dlp、gallery-dl（未實測） | 影片經伺服器 |

IG 和 Threads 的媒體網址帶有加密簽名，經過捷徑時可能被重新編碼而失效（`Bad URL hash`），所以改由伺服器原封不動地下載再轉傳。

## API

所有請求都要帶 `key=<金鑰>&url=<貼文連結>`。

| 路徑 | 說明 |
|---|---|
| `GET /` | 健康檢查 |
| `GET /list` | 回傳選單文字 |
| `GET /urls` | 把選到的項目轉成下載網址 |
| `GET /item` | 下載單一項目（影片合併成 mp4） |
| `GET /usage` | 本月流量（只需金鑰） |
| `GET /cookies` | 是否讀到登入用的 cookies，不顯示內容（只需金鑰） |

## 部署（Render）

1. Fork 這個 repo，在 Render 用 `render.yaml` 建立服務（Docker、免費方案）
2. 環境變數：

| 變數 | 說明 |
|---|---|
| `ACCESS_KEY` | 存取金鑰，多組用逗號分開（render.yaml 會自動產生一組） |
| `RENDER_API_KEY` | Render API 金鑰，用來查流量；不設定就不啟用流量煞車 |
| `BANDWIDTH_LIMIT_MB` | 流量上限，預設 4500 |

3. 要下載 IG、Threads 的話：用小號登入後，以瀏覽器擴充功能匯出 Netscape 格式的 cookies，在 Render 的 **Secret Files** 新增 `cookies.txt`。只匯出 IG 和 Threads 兩個網站，不要用「Export All」，避免上傳到其他帳號的登入資訊
4. 部署完成後，用瀏覽器開 `https://<你的服務>.onrender.com/list?key=<金鑰>&url=<貼文連結>` 測試，能看到選單就成功了

## 使用技術

Python・Flask・Gunicorn・ffmpeg・yt-dlp・gallery-dl・Docker・Render・iOS 捷徑

## 注意事項

- 僅供個人下載公開貼文使用，請尊重原作者的著作權
- 伺服器以一般瀏覽器的方式開啟網頁，不假冒其他網站的機器人，也不繞過人機驗證
- 金鑰和 cookies 只放在 Render 的環境變數與 Secret Files，不進入 repo
- Render 免費方案閒置 15 分鐘會休眠，第一次使用需要等待約 1 分鐘喚醒

// The Ramble Register — iOS home/lock-screen widget (Scriptable)
// =============================================================================
// A glanceable "what's outside my window" tile: the latest bird's photo, when
// it was heard, today's tally, and a little health dot for the listener.
//
// SETUP
//   1. Install "Scriptable" from the App Store (free).
//   2. Open Scriptable → ＋ (new script) → paste this whole file in.
//      Name it "Ramble Register".
//   3. FEED_URL and DASHBOARD_URL below are pre-filled with this project's
//      public feed and dashboard — leave them as-is to track the live Ramble
//      Register, or point them at your own if you run a separate instance.
//   4. Run it once inside Scriptable to check it renders (and to let it ask for
//      network permission).
//   5. Long-press the home screen → ＋ → Scriptable → pick a size (Medium looks
//      best) → add it. Long-press the placed widget → Edit Widget → Script:
//      "Ramble Register".
//
// The OS decides how often widgets refresh (typically every 5–15 min); the
// feed itself updates about once a minute.
// =============================================================================

// Public feed published by the listener (override if you run your own instance).
const FEED_URL = "https://uws-birdwatcher.s3.amazonaws.com/birdwatcher/latest.json";

// Tapping the widget opens this URL (the dashboard). Set it to "" to disable
// tap-to-open, or point it at your own deployed Streamlit app.
const DASHBOARD_URL = "https://danhillary-ramble-register.share.connect.posit.cloud/";

const DOT = {
  green: "#4ccb76",
  yellow: "#e8b04b",
  red: "#e0584f",
  unknown: "#9bb0a8",
};

// Make tapping the widget open the dashboard (no-op if DASHBOARD_URL is blank,
// so clearing it leaves the widget purely glanceable).
//
// iOS hands an https:// tap to whatever the *default* browser is (Chrome, etc.).
// To always land in Safari — where the dashboard's saved-to-Home-Screen layout
// lives and renders best — we swap the scheme to x-safari-https://, which forces
// Safari regardless of the default browser. (Custom schemes like shortcuts:// are
// passed through untouched so you can still point DASHBOARD_URL at a Shortcut.)
function linkToDashboard(w) {
  if (!DASHBOARD_URL) return;
  w.url = DASHBOARD_URL.startsWith("https://")
    ? "x-safari-" + DASHBOARD_URL
    : DASHBOARD_URL;
}

async function loadFeed() {
  const req = new Request(FEED_URL);
  req.headers = { "Cache-Control": "no-cache" };
  return await req.loadJSON();
}

async function loadImage(url) {
  try {
    return await new Request(url).loadImage();
  } catch (e) {
    return null;
  }
}

function buildErrorWidget(message) {
  const w = new ListWidget();
  w.backgroundColor = new Color("#0f2a22");
  w.setPadding(16, 16, 16, 16);
  const t = w.addText("🐦 The Ramble Register");
  t.font = Font.semiboldSystemFont(13);
  t.textColor = Color.white();
  w.addSpacer(6);
  const m = w.addText(message);
  m.font = Font.systemFont(11);
  m.textColor = new Color("#bcd6cb");
  linkToDashboard(w);
  return w;
}

async function buildWidget() {
  let feed;
  try {
    feed = await loadFeed();
  } catch (e) {
    return buildErrorWidget("Couldn't load the feed. Check FEED_URL and your connection.");
  }

  const w = new ListWidget();
  w.backgroundColor = new Color("#0f2a22");
  w.setPadding(14, 16, 14, 16);

  const latest = feed.latest;

  // Full-bleed bird photo with a dark gradient so text stays legible.
  if (latest && latest.photo) {
    const img = await loadImage(latest.photo);
    if (img) {
      w.backgroundImage = img;
      const g = new LinearGradient();
      g.colors = [new Color("#000000", 0.15), new Color("#000000", 0.78)];
      g.locations = [0.0, 1.0];
      w.backgroundGradient = g;
    }
  }

  // Top row: kicker + listener health dot.
  const top = w.addStack();
  top.centerAlignContent();
  const kicker = top.addText("THE RAMBLE REGISTER");
  kicker.font = Font.semiboldSystemFont(9);
  kicker.textColor = new Color("#dceee6");
  top.addSpacer();
  const state = (feed.listener && feed.listener.state) || "unknown";
  const dot = top.addText("●");
  dot.font = Font.systemFont(13);
  dot.textColor = new Color(DOT[state] || DOT.unknown);

  w.addSpacer();

  // Headline: the latest bird.
  if (latest) {
    const name = w.addText(latest.name);
    name.font = Font.boldSystemFont(20);
    name.textColor = Color.white();
    name.lineLimit = 2;
    name.minimumScaleFactor = 0.7;

    const bits = [];
    if (latest.ago) bits.push(`heard ${latest.ago} ago`);
    if (typeof latest.confidence === "number") bits.push(`${latest.confidence}%`);
    const sub = w.addText(bits.join(" · "));
    sub.font = Font.systemFont(11);
    sub.textColor = new Color("#cfe3d9");
  } else {
    const name = w.addText("Listening…");
    name.font = Font.boldSystemFont(20);
    name.textColor = Color.white();
    const sub = w.addText("No birds heard yet today");
    sub.font = Font.systemFont(11);
    sub.textColor = new Color("#cfe3d9");
  }

  w.addSpacer(6);

  // Footer: today's tally.
  const today = feed.today || {};
  const footer = w.addText(
    `Today · ${today.species || 0} species · ${today.calls || 0} calls`
  );
  footer.font = Font.mediumSystemFont(11);
  footer.textColor = new Color("#a8c7bb");

  // Tapping the widget opens the dashboard.
  linkToDashboard(w);

  // Ask iOS to refresh in ~10 minutes (best-effort; the OS has final say).
  w.refreshAfterDate = new Date(Date.now() + 10 * 60 * 1000);
  return w;
}

const widget = await buildWidget();

if (config.runsInWidget) {
  Script.setWidget(widget);
} else {
  // Tapped inside the Scriptable app — preview the medium layout.
  await widget.presentMedium();
}
Script.complete();

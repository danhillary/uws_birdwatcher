// The Ramble Register — iOS home/lock-screen widget (Scriptable)
// =============================================================================
// A glanceable "what's outside my window" tile: the latest bird's photo, when
// it was heard, today's tally, and a little health dot for the listener.
//
// SETUP
//   1. Install "Scriptable" from the App Store (free).
//   2. Open Scriptable → ＋ (new script) → paste this whole file in.
//      Name it "Ramble Register".
//   3. Set FEED_URL below to your public latest.json URL (the S3 object the
//      listener publishes — see the project README, "iPhone widget").
//   4. Run it once inside Scriptable to check it renders (and to let it ask for
//      network permission).
//   5. Long-press the home screen → ＋ → Scriptable → pick a size (Medium looks
//      best) → add it. Long-press the placed widget → Edit Widget → Script:
//      "Ramble Register".
//
// The OS decides how often widgets refresh (typically every 5–15 min); the
// feed itself updates about once a minute.
// =============================================================================

const FEED_URL = "https://YOUR-BUCKET.s3.amazonaws.com/birdwatcher/latest.json";

const DOT = {
  green: "#4ccb76",
  yellow: "#e8b04b",
  red: "#e0584f",
  unknown: "#9bb0a8",
};

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

const $ = (id) => document.getElementById(id);

// ---------- Parts of the page ----------

const headerTools = $("header-tools");
const accountButton = $("account-button");
const searchInput = $("search");

const authView = $("auth-view");
const authForm = $("auth-form");
const authTitle = $("auth-title");
const authIntro = $("auth-intro");
const authUsername = $("auth-username");
const authPassword = $("auth-password");
const authError = $("auth-error");
const authSubmit = $("auth-submit");

const libraryView = $("library-view");
const grid = $("grid");
const statusText = $("status");
const libraryTitle = $("library-title");
const libraryCount = $("library-count");
const continueSection = $("continue");
const continueGrid = $("continue-grid");
const allTitle = $("all-title");

const player = $("player");
const playerTitle = $("player-title");
const playerClose = $("player-close");
const playerError = $("player-error");
const video = $("video");
const qualityWrap = $("quality-wrap");
const qualitySelect = $("quality");

const account = $("account");
const adminSection = $("admin-section");
const userList = $("user-list");
const passwordForm = $("password-form");
const passwordMessage = $("password-message");
const userForm = $("user-form");
const userMessage = $("user-message");

let currentUser = null;      // who is signed in right now (null means nobody)
let authMode = "login";      // "login" or "setup" (creating the very first account)
let hls = null;              // the hls.js player for the movie that's open (if any)
let latestRequest = 0;       // used to ignore old search results

let openMovie = null;        // the movie in the player right now (null when it's closed)
let pendingStart = 0;        // where a non-HLS video should jump to once it has loaded
let lastSavedTime = 0;       // the position we last sent to the server

// ---------- Talking to the server ----------

// One helper for every request. It never throws: it always returns
// { ok, status, data }, and status 0 means the server couldn't be reached.
// keepalive lets a request finish even while the page is closing.
async function api(path, { method = "GET", body, keepalive = false } = {}) {
  try {
    const response = await fetch(path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
      keepalive,
    });
    let data = null;
    try {
      data = await response.json();
    } catch {
      // the reply had no JSON in it
    }
    return { ok: response.ok, status: response.status, data };
  } catch {
    return { ok: false, status: 0, data: null };
  }
}

function errorText(result, fallback) {
  if (result.status === 0) {
    return "Can't reach the HomeStream server. Check that it's running, then try again.";
  }
  const detail = result.data && result.data.detail;
  return typeof detail === "string" ? detail : fallback;
}

function setMessage(element, text, kind = "error") {
  element.textContent = text;
  element.className = kind === "ok" ? "form-ok" : "form-error";
  element.hidden = !text;
}

const SESSION_ENDED = "Your session ended. Please sign in again.";

// ---------- Signing in ----------

function showAuthView(mode, notice = "") {
  // Whatever was open must close, so nothing stays playing for a signed-out person
  if (player.open) closePlayer(false);
  if (account.open) account.close();

  authMode = mode;
  currentUser = null;
  libraryView.hidden = true;
  headerTools.hidden = true;
  authView.hidden = false;

  const setup = mode === "setup";
  authTitle.textContent = setup ? "Create the first account" : "Sign in";
  authIntro.textContent = setup
    ? "This account will be the admin. Once you're in, you can add accounts for your family."
    : "Sign in to watch your library.";
  authPassword.autocomplete = setup ? "new-password" : "current-password";
  authSubmit.textContent = setup ? "Create account" : "Sign in";

  authForm.reset();
  setMessage(authError, notice);
  authUsername.focus();
}

function enterLibrary(user) {
  currentUser = user;
  authView.hidden = true;
  libraryView.hidden = false;
  headerTools.hidden = false;
  accountButton.textContent = user.username;
  accountButton.setAttribute("aria-label", `Account: ${user.username}`);
  searchInput.value = "";
  loadLibrary();
}

authForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const username = authUsername.value.trim();
  const password = authPassword.value;

  if (!username || !password) {
    setMessage(authError, "Enter your username and password.");
    return;
  }

  authSubmit.disabled = true;
  const path = authMode === "setup" ? "/api/auth/setup" : "/api/auth/login";
  const result = await api(path, { method: "POST", body: { username, password } });
  authSubmit.disabled = false;

  if (!result.ok) {
    setMessage(authError, errorText(result, "Something went wrong. Try again."));
    authPassword.value = "";
    authPassword.focus();
    return;
  }

  authForm.reset();
  enterLibrary(result.data);
});

async function start() {
  const status = await api("/api/auth/status");
  if (!status.ok) {
    showAuthView("login", errorText(status, "The server had a problem. Try again in a moment."));
    return;
  }
  if (status.data.setup_needed) {
    showAuthView("setup");
    return;
  }
  const me = await api("/api/auth/me");
  if (me.ok) {
    enterLibrary(me.data);
  } else {
    showAuthView("login");
  }
}

// ---------- The account panel ----------

accountButton.addEventListener("click", () => {
  $("account-name").textContent = currentUser.username;
  passwordForm.reset();
  setMessage(passwordMessage, "");
  setMessage(userMessage, "");
  adminSection.hidden = !currentUser.is_admin;
  if (currentUser.is_admin) loadUsers();
  account.showModal();
});

$("account-close").addEventListener("click", () => account.close());

$("signout").addEventListener("click", async () => {
  account.close();
  await api("/api/auth/logout", { method: "POST" });
  grid.innerHTML = "";
  continueGrid.innerHTML = "";
  showAuthView("login");
});

passwordForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const result = await api("/api/auth/password", {
    method: "POST",
    body: {
      current_password: $("current-password").value,
      new_password: $("new-password").value,
    },
  });

  if (result.status === 401) return showAuthView("login", SESSION_ENDED);
  if (!result.ok) {
    return setMessage(passwordMessage, errorText(result, "Couldn't change the password."));
  }
  passwordForm.reset();
  setMessage(passwordMessage, "Password changed. Your other devices were signed out.", "ok");
});

async function loadUsers() {
  const result = await api("/api/users");
  if (result.status === 401) return showAuthView("login", SESSION_ENDED);
  if (!result.ok) {
    return setMessage(userMessage, errorText(result, "Couldn't load the accounts."));
  }

  userList.replaceChildren();
  for (const user of result.data.users) {
    const isYou = user.id === currentUser.id;

    const item = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = user.username + (user.is_admin ? ", admin" : "") + (isYou ? " (you)" : "");
    item.appendChild(name);

    if (!isYou) {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "link-button";
      remove.textContent = "Delete";
      remove.setAttribute("aria-label", `Delete ${user.username}`);
      remove.addEventListener("click", () => deleteUser(user));
      item.appendChild(remove);
    }
    userList.appendChild(item);
  }
}

async function deleteUser(user) {
  if (!confirm(`Delete the account "${user.username}"? Their watch history goes too, and they'll be signed out right away.`)) return;

  const result = await api(`/api/users/${user.id}`, { method: "DELETE" });
  if (!result.ok) {
    return setMessage(userMessage, errorText(result, "Couldn't delete that account."));
  }
  setMessage(userMessage, `Deleted ${user.username}.`, "ok");
  loadUsers();
}

userForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const username = $("new-username").value.trim();
  const result = await api("/api/users", {
    method: "POST",
    body: { username, password: $("new-user-password").value },
  });

  if (result.status === 401) return showAuthView("login", SESSION_ENDED);
  if (!result.ok) {
    return setMessage(userMessage, errorText(result, "Couldn't add that account."));
  }
  userForm.reset();
  setMessage(userMessage, `Added ${username}. Tell them the password so they can sign in.`, "ok");
  loadUsers();
});

// ---------- Loading the library ----------

// Loads the movie list and, when nobody is searching, the "continue watching" row too
async function loadLibrary(search = "") {
  const requestNumber = ++latestRequest;

  // encodeURIComponent turns spaces into %20 and makes the text safe for a URL
  const listUrl = search
    ? `/api/movies?search=${encodeURIComponent(search)}`
    : "/api/movies";

  const [list, shelf] = await Promise.all([
    api(listUrl),
    search ? null : api("/api/continue"),
  ]);
  if (requestNumber !== latestRequest) return; // a newer search is on the way

  if (list.status === 401 || (shelf && shelf.status === 401)) {
    return showAuthView("login", SESSION_ENDED);
  }
  if (!list.ok) {
    grid.innerHTML = "";
    showShelf([]);
    showStatus(errorText(list, "The server had a problem loading your movies. Refresh the page to try again."));
    return;
  }

  showShelf(shelf && shelf.ok ? shelf.data.movies : []);
  showMovies(list.data.movies, search);
}

// ---------- Showing movies on the page ----------

function showShelf(movies) {
  continueGrid.replaceChildren(...movies.map(createCard));
  continueSection.hidden = movies.length === 0;
  allTitle.hidden = movies.length === 0;
}

function showMovies(movies, search) {
  grid.innerHTML = "";
  libraryTitle.textContent = search ? `Results for “${search}”` : "Your movies";
  libraryCount.textContent = movies.length === 1 ? "1 movie" : `${movies.length} movies`;

  if (movies.length === 0) {
    showStatus(search
      ? `No movies match “${search}”. Try a shorter word.`
      : "No movies yet. Add video files to the media/movies folder, then refresh this page.");
    return;
  }

  hideStatus();
  for (const movie of movies) {
    grid.appendChild(createCard(movie));
  }
}

// "1 h 12 min left", "45 min left"
function formatRemaining(seconds) {
  const minutes = Math.max(1, Math.ceil(seconds / 60));
  if (minutes < 60) return `${minutes} min left`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest === 0 ? `${hours} h left` : `${hours} h ${rest} min left`;
}

function createCard(movie) {
  const card = document.createElement("button");
  card.className = "card";
  card.type = "button";

  // Build a poster color from the movie ID, so each movie always gets the same color
  const hue = parseInt(movie.id.slice(0, 6), 16) % 360;

  const poster = document.createElement("div");
  poster.className = "poster";
  poster.style.setProperty("--hue", hue);

  // The colored title poster is the fallback when there is no picture (or it fails to load)
  const showTitlePoster = () => {
    poster.querySelector("img")?.remove();
    if (movie.year) {
      const year = document.createElement("span");
      year.className = "poster-year";
      year.textContent = movie.year;
      poster.prepend(year);
    }
    const posterTitle = document.createElement("span");
    posterTitle.className = movie.title.length > 18 ? "poster-title long" : "poster-title";
    posterTitle.textContent = movie.title;
    poster.prepend(posterTitle);
  };

  if (movie.thumbnail_url) {
    const picture = document.createElement("img");
    picture.src = movie.thumbnail_url;
    picture.alt = ""; // the title is written right below, so screen readers don't need it twice
    picture.loading = "lazy"; // only download pictures when they scroll into view
    picture.addEventListener("error", showTitlePoster);
    poster.appendChild(picture);
  } else {
    showTitlePoster();
  }

  // How far this person has watched, drawn as a thin bar along the bottom of the picture
  const progress = movie.progress;
  if (progress && progress.resumable) {
    const bar = document.createElement("div");
    bar.className = "progress";
    const fill = document.createElement("div");
    fill.className = "progress-fill";
    fill.style.width = `${Math.min(100, (progress.position / progress.duration) * 100)}%`;
    bar.appendChild(fill);
    poster.appendChild(bar);
  }

  const title = document.createElement("p");
  title.className = "card-title";
  title.textContent = movie.title;

  const meta = document.createElement("p");
  meta.className = "card-meta";
  const kind = movie.hls_url ? "Adaptive quality" : movie.format.toUpperCase();
  meta.textContent = movie.year ? `${movie.year}, ${kind}` : kind;

  card.append(poster, title, meta);

  if (progress && progress.resumable) {
    const left = document.createElement("p");
    left.className = "card-progress";
    left.textContent = formatRemaining(progress.duration - progress.position);
    card.appendChild(left);
  } else if (progress && progress.finished) {
    const watched = document.createElement("p");
    watched.className = "card-progress";
    watched.textContent = "Watched";
    card.appendChild(watched);
  }

  card.addEventListener("click", () => openPlayer(movie));
  return card;
}

function showStatus(message) {
  statusText.textContent = message;
  statusText.hidden = false;
}

function hideStatus() {
  statusText.hidden = true;
}

// ---------- The video player ----------

function openPlayer(movie) {
  playerTitle.textContent = movie.title;
  playerError.hidden = true;
  qualityWrap.hidden = true;
  video.hidden = false;
  player.showModal();

  // Pick up where this person stopped. A finished movie starts again from the beginning.
  const progress = movie.progress;
  const startAt = progress && progress.resumable ? progress.position : 0;

  openMovie = movie;
  lastSavedTime = startAt;
  pendingStart = 0;

  if (movie.hls_url && window.Hls && Hls.isSupported()) {
    // Most browsers: hls.js reads the playlist and feeds the segments to the video
    hls = new Hls({ startPosition: startAt });
    hls.loadSource(movie.hls_url);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      buildQualityMenu();
      playVideo();
    });
    hls.on(Hls.Events.LEVEL_SWITCHED, updateAutoLabel);
    hls.on(Hls.Events.ERROR, (event, data) => {
      if (!data.fatal) return;
      if (data.response && data.response.code === 401) {
        showAuthView("login", SESSION_ENDED);
        return;
      }
      showPlayerError("This movie couldn't load. Check that the server is running, then try again.");
    });
  } else if (movie.hls_url && video.canPlayType("application/vnd.apple.mpegurl")) {
    // Safari and iPhone can play HLS on their own
    pendingStart = startAt;
    video.src = movie.hls_url;
    playVideo();
  } else {
    // Not converted yet: play the original file with range requests (Stage 4)
    pendingStart = startAt;
    video.src = movie.stream_url;
    playVideo();
  }
}

// Once the video knows how long it is, jump to the saved spot (hls.js handles this itself)
video.addEventListener("loadedmetadata", () => {
  if (pendingStart > 0) video.currentTime = pendingStart;
  pendingStart = 0;
});

function playVideo() {
  video.play().catch(() => {
    // Some browsers block autoplay; the person can still press play
  });
}

function buildQualityMenu() {
  qualitySelect.innerHTML = "";

  const auto = document.createElement("option");
  auto.value = "-1";
  auto.textContent = "Auto";
  qualitySelect.appendChild(auto);

  // Highest quality first
  const levels = hls.levels
    .map((level, index) => ({ index, height: level.height }))
    .sort((a, b) => b.height - a.height);

  for (const level of levels) {
    const option = document.createElement("option");
    option.value = String(level.index);
    option.textContent = `${level.height}p`;
    qualitySelect.appendChild(option);
  }

  qualitySelect.value = "-1";
  qualityWrap.hidden = levels.length < 2;
}

function updateAutoLabel(event, data) {
  // In Auto mode, show which quality hls.js picked, like "Auto (720p)"
  const autoOption = qualitySelect.querySelector('option[value="-1"]');
  if (!autoOption || !hls) return;
  const height = hls.levels[data.level].height;
  autoOption.textContent = hls.autoLevelEnabled ? `Auto (${height}p)` : "Auto";
}

// -1 means Auto; any other number locks the player to that quality
qualitySelect.addEventListener("change", () => {
  if (hls) hls.currentLevel = Number(qualitySelect.value);
});

function showPlayerError(message) {
  playerError.textContent = message;
  video.hidden = true;
  playerError.hidden = false;
}

// ---------- Remembering where you stopped ----------

// Sends the current spot to the server. It reads the position right now (before
// anything else can change it) and returns the request, so callers can wait for it.
function saveProgress({ keepalive = false } = {}) {
  if (!openMovie || !Number.isFinite(video.duration) || video.duration <= 0) return null;

  const position = video.currentTime;
  lastSavedTime = position;
  return api(`/api/progress/${openMovie.id}`, {
    method: "PUT",
    body: { position, duration: video.duration },
    keepalive,
  });
}

// While playing: save every 10 seconds of movie time. Jumping far ahead or back counts too.
video.addEventListener("timeupdate", () => {
  if (openMovie && Math.abs(video.currentTime - lastSavedTime) >= 10) saveProgress();
});

video.addEventListener("pause", () => saveProgress());
video.addEventListener("ended", () => saveProgress());

// Closing the tab, or switching away on a phone, may be the last chance to save
window.addEventListener("pagehide", () => saveProgress({ keepalive: true }));
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") saveProgress({ keepalive: true });
});

async function closePlayer(save = true) {
  // Read the position first, before the video is taken apart
  const finalSave = save ? saveProgress() : null;
  openMovie = null;

  video.pause();
  if (hls) {
    hls.destroy();
    hls = null;
  }
  video.removeAttribute("src");
  video.load(); // stops the browser from downloading more of the video
  player.close();

  // Once the server has the final position, redraw so the bars and the row are up to date
  if (finalSave) {
    await finalSave;
    loadLibrary(searchInput.value.trim());
  }
}

video.addEventListener("error", () => {
  if (!video.getAttribute("src") || hls) return; // ignore errors from closing, hls.js reports its own
  showPlayerError("Your browser can't play this file format. Run convert.py to make a version that plays everywhere.");
});

playerClose.addEventListener("click", () => closePlayer());

// Pressing Escape closes a dialog; make sure the video stops (and saves) too
player.addEventListener("cancel", (event) => {
  event.preventDefault();
  closePlayer();
});

// ---------- Search ----------

// Wait until the person stops typing for 300 ms before asking the server.
// This is called "debouncing" and avoids sending a request for every key press.
let searchTimer;
searchInput.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadLibrary(searchInput.value.trim()), 300);
});

// Find out who is visiting, then show the right screen
start();
"""
Automated tests for HomeStream.

Run them from the project folder with:  python -m pytest

Every test gets a brand new empty database and movies folder in a temporary
directory, so the tests never touch your real movies or your real accounts.
"""

import shutil

import pytest
from fastapi.testclient import TestClient

import auth
import db
import main

LOCAL = ("127.0.0.1", 50000)      # a request coming from the laptop itself
PHONE = ("192.168.1.50", 50000)   # a request coming from another device on the network
STRONG = "correct horse battery"


# ---------- Setup helpers ----------

@pytest.fixture()
def movies_dir(tmp_path, monkeypatch):
    """Point the app at temporary folders and an empty database."""
    for folder in ("movies", "hls", "thumbs"):
        (tmp_path / folder).mkdir()
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(main, "MOVIES_DIR", tmp_path / "movies")
    monkeypatch.setattr(main, "HLS_DIR", tmp_path / "hls")
    monkeypatch.setattr(main, "THUMBS_DIR", tmp_path / "thumbs")
    auth._failed_logins.clear()
    return tmp_path / "movies"


@pytest.fixture()
def laptop(movies_dir):
    with TestClient(main.app, client=LOCAL) as client:
        yield client


@pytest.fixture()
def phone(movies_dir):
    with TestClient(main.app, client=PHONE) as client:
        yield client


@pytest.fixture()
def admin(laptop):
    """The laptop, already signed in as the admin."""
    response = laptop.post("/api/auth/setup", json={"username": "asha", "password": STRONG})
    assert response.status_code == 200
    return laptop


def add_movie(movies_dir, name, size=2000):
    path = movies_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def movie_id(client, title):
    movies = client.get("/api/movies").json()["movies"]
    return next(m["id"] for m in movies if m["title"] == title)


def titles_of(response):
    return [m["title"] for m in response.json()["movies"]]


def titles(client, query=""):
    return [m["title"] for m in client.get(f"/api/movies{query}").json()["movies"]]


def sign_in_family_member(admin, phone, name="mom", password="family-pass-1"):
    """The admin creates an account and the person signs in on their phone."""
    assert admin.post("/api/users", json={"username": name, "password": password}).status_code == 200
    assert phone.post("/api/auth/login", json={"username": name, "password": password}).status_code == 200
    return phone


def count_rows(sql):
    with db.open_db() as conn:
        return conn.execute(sql).fetchone()[0]


# ---------- Passwords ----------

def test_passwords_are_hashed_and_verified():
    hashed = auth.hash_password("secret-password")
    assert hashed.startswith("scrypt$")
    assert "secret-password" not in hashed
    assert auth.verify_password("secret-password", hashed)
    assert not auth.verify_password("wrong-password", hashed)
    assert auth.hash_password("secret-password") != hashed  # a random salt makes every hash different


# ---------- Who can get in ----------

def test_strangers_are_blocked_but_the_web_page_is_public(phone):
    for path in ("/api/movies", "/api/continue", "/api/stream/abc", "/hls/abc/master.m3u8", "/thumbs/abc.jpg"):
        assert phone.get(path).status_code == 401, path
    assert phone.get("/").status_code == 200
    assert phone.get("/api/health").status_code == 200


def test_first_account_can_only_be_created_on_the_laptop(laptop, phone):
    body = {"username": "asha", "password": STRONG}
    assert phone.post("/api/auth/setup", json=body).status_code == 403
    assert laptop.post("/api/auth/setup", json=body).status_code == 200
    # once the first account exists, setup closes for good
    assert laptop.post("/api/auth/setup", json={"username": "other_one", "password": STRONG}).status_code == 403
    assert laptop.get("/api/auth/status").json() == {"setup_needed": False}


def test_username_and_password_rules(laptop):
    assert laptop.post("/api/auth/setup", json={"username": "a b", "password": STRONG}).status_code == 400
    assert laptop.post("/api/auth/setup", json={"username": "asha", "password": "short"}).status_code == 400


def test_login_and_logout(admin, phone):
    sign_in_family_member(admin, phone)
    assert phone.get("/api/auth/me").json()["username"] == "mom"
    phone.post("/api/auth/logout")
    assert phone.get("/api/movies").status_code == 401


def test_login_errors_do_not_reveal_who_has_an_account(admin, phone):
    wrong_password = phone.post("/api/auth/login", json={"username": "asha", "password": "nope-nope-nope"})
    unknown_user = phone.post("/api/auth/login", json={"username": "ghost", "password": "nope-nope-nope"})
    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json() == unknown_user.json()


def test_five_wrong_passwords_lock_the_login(admin, phone):
    for _ in range(5):
        phone.post("/api/auth/login", json={"username": "asha", "password": "wrong-wrong-1"})
    response = phone.post("/api/auth/login", json={"username": "asha", "password": STRONG})
    assert response.status_code == 429


def test_only_the_admin_manages_accounts(admin, phone):
    sign_in_family_member(admin, phone)
    assert phone.get("/api/users").status_code == 403
    assert phone.post("/api/users", json={"username": "evil", "password": STRONG}).status_code == 403
    assert phone.delete("/api/users/1").status_code == 403
    assert admin.get("/api/users").status_code == 200
    assert admin.delete("/api/users/1").status_code == 400   # nobody can delete their own account
    # usernames are unique, whatever the capital letters
    assert admin.post("/api/users", json={"username": "MOM", "password": STRONG}).status_code == 409


def test_changing_a_password_signs_out_other_devices(admin, phone):
    sign_in_family_member(admin, phone)
    with TestClient(main.app, client=("192.168.1.60", 50000)) as tablet:
        tablet.post("/api/auth/login", json={"username": "mom", "password": "family-pass-1"})
        change = phone.post("/api/auth/password",
                            json={"current_password": "family-pass-1", "new_password": "brand-new-pass"})
        assert change.status_code == 200
        assert phone.get("/api/auth/me").status_code == 200     # this device stays signed in
        assert tablet.get("/api/auth/me").status_code == 401    # the other one is signed out


# ---------- The movie library ----------

def test_library_follows_the_movies_folder(admin, movies_dir):
    add_movie(movies_dir, "Zodiac.2007.mp4")
    add_movie(movies_dir, "Inception (2010)/inception.mp4")

    movies = admin.get("/api/movies").json()["movies"]
    assert [m["title"] for m in movies] == ["Inception", "Zodiac"]   # sorted by title
    assert movies[0]["year"] == 2010
    assert "relative_path" not in movies[0]                           # real file paths stay private

    add_movie(movies_dir, "New.Film.2022.mp4")
    assert "New Film" in titles(admin)
    (movies_dir / "New.Film.2022.mp4").unlink()
    assert "New Film" not in titles(admin)


def test_search_ignores_capital_letters(admin, movies_dir):
    add_movie(movies_dir, "Zodiac.2007.mp4")
    add_movie(movies_dir, "Inception.2010.mp4")
    assert titles(admin, "?search=ZOD") == ["Zodiac"]
    assert titles(admin, "?search=nothing") == []


def test_streaming_supports_range_requests(admin, movies_dir):
    add_movie(movies_dir, "Zodiac.2007.mp4", size=5000)
    mid = movie_id(admin, "Zodiac")

    whole = admin.get(f"/api/stream/{mid}")
    assert whole.status_code == 200 and len(whole.content) == 5000

    part = admin.get(f"/api/stream/{mid}", headers={"Range": "bytes=100-199"})
    assert part.status_code == 206
    assert part.headers["content-range"] == "bytes 100-199/5000"
    assert len(part.content) == 100

    assert admin.get(f"/api/stream/{mid}", headers={"Range": "bytes=9999-"}).status_code == 416
    assert admin.get("/api/stream/nope").status_code == 404


# ---------- Watch progress ----------

def test_progress_is_saved_resumed_and_finished(admin, movies_dir):
    add_movie(movies_dir, "Zodiac.2007.mp4")
    mid = movie_id(admin, "Zodiac")
    assert admin.get(f"/api/movies/{mid}").json()["progress"] is None

    admin.put(f"/api/progress/{mid}", json={"position": 240, "duration": 600})
    progress = admin.get(f"/api/movies/{mid}").json()["progress"]
    assert progress == {"position": 240.0, "duration": 600.0, "finished": False, "resumable": True}
    assert titles_of(admin.get("/api/continue")) == ["Zodiac"]

    finished = admin.put(f"/api/progress/{mid}", json={"position": 580, "duration": 600})
    assert finished.json()["finished"] is True                          # 96% counts as finished
    assert titles_of(admin.get("/api/continue")) == []
    assert admin.get(f"/api/movies/{mid}").json()["progress"]["resumable"] is False


def test_a_few_seconds_of_watching_is_not_continue_watching(admin, movies_dir):
    add_movie(movies_dir, "Zodiac.2007.mp4")
    mid = movie_id(admin, "Zodiac")
    admin.put(f"/api/progress/{mid}", json={"position": 4, "duration": 600})
    assert titles_of(admin.get("/api/continue")) == []


def test_progress_rejects_nonsense(admin, movies_dir):
    add_movie(movies_dir, "Zodiac.2007.mp4")
    mid = movie_id(admin, "Zodiac")
    assert admin.put(f"/api/progress/{mid}", json={"position": -5, "duration": 600}).status_code == 422
    assert admin.put(f"/api/progress/{mid}", json={"position": 5, "duration": 0}).status_code == 422
    assert admin.put("/api/progress/nope", json={"position": 5, "duration": 60}).status_code == 404


def test_continue_watching_shows_the_most_recent_first(admin, movies_dir):
    add_movie(movies_dir, "Alpha.2001.mp4")
    add_movie(movies_dir, "Beta.2002.mp4")
    alpha, beta = movie_id(admin, "Alpha"), movie_id(admin, "Beta")

    def set_age(mid, updated_at):
        with db.open_db() as conn:
            conn.execute("UPDATE progress SET updated_at = ? WHERE movie_id = ?", (updated_at, mid))

    admin.put(f"/api/progress/{alpha}", json={"position": 100, "duration": 600})
    admin.put(f"/api/progress/{beta}", json={"position": 100, "duration": 600})
    set_age(alpha, 1_000)                                    # Alpha was watched long ago
    assert titles_of(admin.get("/api/continue")) == ["Beta", "Alpha"]

    admin.put(f"/api/progress/{alpha}", json={"position": 200, "duration": 600})   # watched again now
    set_age(beta, 2_000)
    assert titles_of(admin.get("/api/continue")) == ["Alpha", "Beta"]


def test_each_person_has_their_own_history(admin, phone, movies_dir):
    add_movie(movies_dir, "Zodiac.2007.mp4")
    mid = movie_id(admin, "Zodiac")
    sign_in_family_member(admin, phone)

    admin.put(f"/api/progress/{mid}", json={"position": 30, "duration": 7000})
    assert phone.get(f"/api/movies/{mid}").json()["progress"] is None
    assert titles_of(phone.get("/api/continue")) == []

    # Someone sneaking another user's id into the request must not change that user's history
    phone.put(f"/api/progress/{mid}", json={"position": 1000, "duration": 7000, "user_id": 1})
    assert admin.get(f"/api/movies/{mid}").json()["progress"]["position"] == 30.0


def test_deleting_a_user_removes_their_history(admin, phone, movies_dir):
    add_movie(movies_dir, "Zodiac.2007.mp4")
    mid = movie_id(admin, "Zodiac")
    sign_in_family_member(admin, phone)
    mom_id = phone.get("/api/auth/me").json()["id"]
    phone.put(f"/api/progress/{mid}", json={"position": 100, "duration": 600})
    assert count_rows("SELECT COUNT(*) FROM progress") == 1

    assert admin.delete(f"/api/users/{mom_id}").status_code == 200
    assert count_rows("SELECT COUNT(*) FROM progress") == 0
    assert phone.get("/api/movies").status_code == 401        # and their sign-in stops working at once


def test_a_missing_movies_folder_does_not_wipe_history(admin, movies_dir):
    add_movie(movies_dir, "Zodiac.2007.mp4")
    mid = movie_id(admin, "Zodiac")
    admin.put(f"/api/progress/{mid}", json={"position": 100, "duration": 600})

    shutil.rmtree(movies_dir)                                  # for example, an unplugged drive
    assert admin.get("/api/movies").status_code == 200
    assert count_rows("SELECT COUNT(*) FROM progress") == 1


# ---------- Safety ----------

def test_sql_injection_attempts_do_nothing(admin, movies_dir):
    add_movie(movies_dir, "Zodiac.2007.mp4")
    assert admin.get("/api/movies/x' OR '1'='1").status_code == 404
    response = admin.put("/api/progress/x'; DROP TABLE users;--", json={"position": 5, "duration": 60})
    assert response.status_code == 404
    assert count_rows("SELECT COUNT(*) FROM users") == 1       # the users table is still there
    assert admin.get("/api/auth/me").status_code == 200

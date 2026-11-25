"""iCourse Subscriber — top-level orchestration.

The runtime is split across cooperating components — Scheduler (pools +
resource monitor), Reporter (centralised logging), PPTPipeline, LectureRunner,
AudioDownloader.  This file does only orchestration:

  1. Build all components.
  2. Login + enumerate.
  3. Drive LectureRunner across the queued lectures.
  4. Email + bookkeeping.
  5. Shutdown.

Anything more interesting belongs in one of ``src/*`` modules.
"""

from __future__ import annotations

import argparse
import time
import traceback
from typing import TYPE_CHECKING

from src.runtime import config
from src.data.database import Database
from src.runtime.reporter import Reporter

if TYPE_CHECKING:
    from src.api.emailer import Emailer
    from src.api.icourse import ICourseClient
    from src.api.webvpn import WebVPNSession
    from src.runtime.scheduler import Scheduler


def login_with_retry(max_attempts: int = 10) -> WebVPNSession:
    """Login to WebVPN + iCourse CAS, retrying on transient failures.

    The iCourse CAS step (authenticate_icourse) has its own inner retry
    loop for transient redirect-chain hiccups; this outer loop only runs
    when those inner retries are exhausted, which generally means the
    WebVPN session itself needs a fresh login.  5 attempts handles the
    long tail of times when CAS rejects multiple fresh sessions in a
    row before letting one through.
    """
    from src.api.webvpn import SecondFactorError, WebVPNSession

    for attempt in range(max_attempts):
        try:
            vpn = WebVPNSession()
            print(f"\n[Login] WebVPN (attempt {attempt + 1}/{max_attempts})...")
            vpn.login()
            print("[Login] iCourse CAS...")
            vpn.authenticate_icourse()
            return vpn
        except SecondFactorError:
            vpn.session.close()
            raise
        except Exception as e:
            if attempt < max_attempts - 1:
                print(f"  Failed: {type(e).__name__}: {e}; retrying...")
                time.sleep(5)
            else:
                raise


def _check_session(client: ICourseClient) -> None:
    """Verify WebVPN session; re-login in place if expired.

    Mutates ``client`` so background workers holding the same instance
    automatically pick up refreshed cookies.
    """
    if client.check_alive():
        return
    print("[Session] WebVPN session expired, re-logging in...")
    client.vpn = login_with_retry()
    client._userinfo = None


def _enumerate_lectures(client: ICourseClient, db: Database,
                        reporter: Reporter) -> tuple[list[tuple[str, str, dict]], int]:
    """Sync, fast: list every (course_id, course_title, lecture) we'll
    process this run.  Done up-front so the prefetch loop can see across
    course boundaries when picking the "next" lecture."""
    out: list[tuple[str, str, dict]] = []
    queued_ids: set[str] = set()
    failed_courses = 0
    for course_id in dict.fromkeys(config.COURSE_IDS):
        try:
            _check_session(client)
            detail = client.get_course_detail(course_id)
            course_title = detail["title"]
            teacher = detail["teacher"]
            lectures = detail["lectures"]
            playback_count = sum(1 for l in lectures if l.get("has_playback"))
            reporter.course_header(
                course_id, course_title, teacher,
                total=len(lectures), playback=playback_count,
            )
            db.upsert_course(course_id, course_title, teacher)

            # School system sometimes lists duplicate lectures; dedup the
            # raw list so the same logic produces the same outcome each run.
            # When a sub_title appears more than once, keep the first one
            # that has playback; if none have playback, keep the first.
            seen_sub_titles: dict[str, dict] = {}
            deduped = []
            for lec in lectures:
                title = lec.get("sub_title", "")
                if not title:
                    deduped.append(lec)
                    continue
                existing = seen_sub_titles.get(title)
                if existing is None:
                    seen_sub_titles[title] = lec
                    deduped.append(lec)
                elif not existing.get("has_playback") and lec.get("has_playback"):
                    # replace the earlier no-playback entry in place so the
                    # processing order stays chronological
                    deduped[deduped.index(existing)] = lec
                    seen_sub_titles[title] = lec
                    reporter.course_dedup_skip(title, existing["sub_id"])
                else:
                    reporter.course_dedup_skip(title, lec["sub_id"])
            lectures = deduped

            known_processed = db.get_processed_sub_ids(course_id)
            new_lectures = [
                lec for lec in lectures
                if lec.get("has_playback")
                and str(lec["sub_id"]) not in known_processed
            ]
            unprocessed = db.get_unprocessed_lectures(course_id)
            new_ids = {str(lec["sub_id"]) for lec in new_lectures}
            retry_only = [
                {"sub_id": u["sub_id"], "sub_title": u["sub_title"],
                 "date": u["date"]}
                for u in unprocessed if u["sub_id"] not in new_ids
            ]
            new_lectures.extend(retry_only)
            reporter.course_new_count(len(new_lectures))
            if not new_lectures:
                continue

            for lecture in new_lectures:
                sub_id = str(lecture["sub_id"])
                if sub_id in queued_ids:
                    continue
                db.insert_lecture(
                    sub_id, course_id,
                    lecture.get("sub_title", ""),
                    lecture.get("date", ""),
                )
                out.append((course_id, course_title, lecture))
                queued_ids.add(sub_id)
        except Exception:
            failed_courses += 1
            reporter.course_enumeration_error(course_id)
            traceback.print_exc()
    return out, failed_courses


def _drive_lectures(client: ICourseClient, db: Database,
                    scheduler: Scheduler, reporter: Reporter,
                    all_lectures: list[tuple[str, str, dict]],
                    email_items: list | None) -> int:
    """Phase 2: run each lecture through LectureRunner.

    Pre-schedules the first lecture's prefetch (audio + images) before
    entering the loop; subsequent prefetches are kicked off from inside
    each LectureRunner.run via ``next_info``.
    """
    from src.ai.summarizer import Summarizer
    from src.ai.transcriber import Transcriber
    from src.pipeline.lecture_runner import LectureRunner

    runner = LectureRunner(
        client, db, scheduler, Transcriber(), Summarizer(), reporter,
    )

    first_course, _, first_lec = all_lectures[0]
    runner.prefetch_first(first_course, str(first_lec["sub_id"]))

    failed_lectures = 0
    for i, (course_id, course_title, lecture) in enumerate(all_lectures):
        sub_id = str(lecture["sub_id"])
        next_info: tuple[str, str] | None = None
        if i + 1 < len(all_lectures):
            next_course, _, next_lec = all_lectures[i + 1]
            next_info = (next_course, str(next_lec["sub_id"]))

        _check_session(client)
        try:
            summary = runner.run(
                course_id, course_title, lecture, next_info=next_info,
            )
            if email_items is None and summary is None:
                # Some failures (e.g. missing video) are recorded without
                # raising. Empty transcripts can also be legitimate skips.
                status = db.get_lecture(sub_id)
                if status and status["error_stage"]:
                    failed_lectures += 1
            elif summary and email_items is not None:
                email_items.append({
                    "sub_id": sub_id,
                    "course_title": course_title,
                    "sub_title": lecture.get("sub_title", sub_id),
                    "date": lecture.get("date", ""),
                    "summary": summary,
                })
        except Exception:
            failed_lectures += 1
            reporter.lecture_error(sub_id)
            traceback.print_exc()
        finally:
            # Belt-and-braces: drop any lingering prefetch entry for this
            # lecture so we don't leak bytes if the runner crashed before
            # PPTPipeline.submit released the cache.
            scheduler.image_cache.discard(sub_id)
            scheduler.audio_downloader.release(sub_id)
    return failed_lectures


def _send_email(emailer: Emailer | None, db: Database, reporter: Reporter,
                email_items: list) -> None:
    """Append any previously-processed-but-unsent lectures, then send."""
    unsent = db.get_unsent_lectures()
    if unsent:
        seen_sub_ids = {item["sub_id"] for item in email_items}
        for row in unsent:
            if row["sub_id"] not in seen_sub_ids:
                email_items.append({
                    "sub_id": row["sub_id"],
                    "course_title": row["course_title"],
                    "sub_title": row["sub_title"],
                    "date": row["date"],
                    "summary": row["summary"],
                })
        reporter.email_recovered_unsent(len(unsent))

    if not (emailer and email_items):
        return
    try:
        reporter.email_summary(len(email_items))
        if emailer.send(email_items):
            db.mark_emailed_batch([item["sub_id"] for item in email_items])
        else:
            reporter.email_failed()
    except Exception:
        reporter.info("[Email] Failed to send:")
        traceback.print_exc()


def _crawl_semester_catalog(client: ICourseClient, db: Database,
                            reporter: Reporter) -> None:
    """Discover semesters on every run and fetch catalogs for new terms.

    Compare API term names with the names stored in ``all_courses``.
    Only terms with a successfully fetched catalog are considered known,
    so empty or failed fetches are retried on the next run.
    """
    reporter.info("Discovering available semesters from API...")
    try:
        _check_session(client)
        terms = client.discover_terms()
    except Exception as e:
        reporter.crawl_courses_failed("discovery", e)
        return

    if not terms:
        reporter.info("No semesters found via API discovery.")
        return

    reporter.info(f"Found {len(terms)} semester(s): "
                  f"{', '.join(t['name'] for t in terms)}")

    known_terms = db.list_catalog_terms()
    new_terms = [term for term in terms if term["name"] not in known_terms]
    if not new_terms:
        reporter.info("Skipping catalog crawl (no new semesters).")
        return

    for term_info in new_terms:
        code = term_info["code"]
        name = term_info["name"]
        expected = term_info["count"]
        reporter.crawl_courses_start(name)
        t0 = time.time()
        try:
            _check_session(client)
            rows = client.list_semester_courses(code)
            if not rows:
                reporter.info(f"  Term {name}: API returned 0 courses, skipping.")
                continue
            if len(rows) < expected:
                reporter.info(
                    f"  Term {name}: fetched {len(rows)} of {expected} courses, "
                    "deferring catalog update until the next run."
                )
                continue
            # Pass the human-readable term name (not the API code) to the
            # DB so the frontend displays "2025-20262" instead of "25".
            deleted, upserted = db.upsert_all_courses_for_term(name, rows)
            reporter.crawl_courses_done(
                name, len(rows), deleted, upserted, time.time() - t0,
            )
        except Exception as e:
            reporter.crawl_courses_failed(name, e)
            continue
        reporter.info(f"  ({code}) → {expected} API courses, "
                      f"{len(rows)} fetched")

    reporter.info("Semester catalog crawl complete.")


def run(*, local: bool = False) -> int:
    """Sync once. Local callers use the exit code to report partial failures."""
    reporter = Reporter()
    reporter.run_header()

    if local and not config.COURSE_IDS:
        reporter.info("未订阅课程，本次无需同步。")
        return 0
    if local and not (config.STUDENT_ID and config.PASSWORD):
        reporter.info("缺少复旦登录信息，请配置 StuId 和 UISPsw。")
        return 1

    from src.api.icourse import ICourseClient

    db = Database()
    corrected = db.sync_dates_from_sub()
    if corrected:
        print(f"  [Date] Synced {corrected} lecture date(s) from sub_title", flush=True)
    vpn = login_with_retry()
    client = ICourseClient(vpn)
    if not local:
        _crawl_semester_catalog(client, db, reporter)
        if not config.COURSE_IDS:
            reporter.info("\n[Crawl-only mode] No COURSE_IDS — skipping lectures.")
            reporter.run_footer()
            return 0

    all_lectures, failed_courses = _enumerate_lectures(client, db, reporter)
    email_items = None if local else []
    failed_lectures = 0
    if all_lectures:
        from src.runtime.scheduler import Scheduler

        scheduler = Scheduler(reporter=reporter)
        try:
            failed_lectures = _drive_lectures(
                client, db, scheduler, reporter, all_lectures, email_items,
            )
        finally:
            scheduler.shutdown()

    if local:
        reporter.info(
            f"同步结束。已检查 {len(all_lectures)} 个待处理课次，"
            f"课程读取失败 {failed_courses}，课次处理失败 {failed_lectures}。"
        )
        return int(bool(failed_courses or failed_lectures))

    emailer = None
    if config.SMTP_EMAIL and config.SMTP_PASSWORD:
        from src.api.emailer import Emailer

        emailer = Emailer()
    _send_email(emailer, db, reporter, email_items)
    reporter.run_footer()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the iCourse subscriber once.")
    parser.add_argument("--local", action="store_true",
                        help="Sync subscribed courses into local SQLite without email.")
    if parser.parse_args().local:
        import os
        import sys
        from pathlib import Path

        # Use the same credentials, subscriptions and single-writer lock as the web UI.
        script = Path(__file__).resolve().with_name("local_app.py")
        os.execv(sys.executable, [sys.executable, str(script), "sync"])
    raise SystemExit(run())

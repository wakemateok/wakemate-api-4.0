# app/main.py
from fastapi import APIRouter, FastAPI, HTTPException, Depends, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import text, desc
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.sql import func
from datetime import datetime, timezone
from app import models, schemas
from app.models import (
    User,
    UsersBodyInfo,
    UsersRealSleepData,
    UsersTargetWakingPeriod,
    UsersRealTimeIntake,
    UsersPVTResults,
    RecommendationsCaffeine,
    AlertnessDataForVisualization,
    DeviceHeartRateData,
    DeviceXYZTimeData,
)
from app.schemas import UserLogin
from fastapi.middleware.cors import CORSMiddleware
from uuid import uuid4, UUID
from .database import engine, SessionLocal, get_db
from .auth import router as auth_router, enforce_data_owner, scoped_user_id, cleanup_expired_sessions
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler
import atexit
import os
from fastapi.responses import PlainTextResponse
from app.day_records import replace_day_records
from app.pvt_records import save_pvt_result
from core.period_validation import first_recommendation_overlap, overlap_detail

# 計算模組
from core.caffeine_recommendation import run_caffeine_recommendation
from core.alertness_data import run_alertness_data
from core.database import get_db_connection

app = FastAPI()
protected = APIRouter(dependencies=[Depends(enforce_data_owner)])
app.include_router(auth_router)


class QuestionnaireLinksResponse(BaseModel):
    chinese_baseline_url: str = ""
    chinese_daily_url: str = ""
    indonesian_baseline_url: str = ""
    indonesian_daily_url: str = ""
    # Backward-compatible aliases for older app builds.
    chinese_url: str = ""
    indonesian_url: str = ""
    version: str = "v1"


MAX_SINGLE_CAFFEINE_MG = 500
MAX_SLEEP_DURATION_HOURS = 48
MAX_WAKE_DURATION_HOURS = 48
MIN_PVT_MEAN_RT_MS = 100
MAX_PVT_MEAN_RT_MS = 3000
MAX_PVT_EVENT_COUNT = 100


def _validate_time_range(
    start: datetime | None,
    end: datetime | None,
    label: str,
    max_hours: int,
):
    if start is None or end is None:
        raise HTTPException(status_code=422, detail=f"{label}開始與結束時間皆為必填")
    if not end > start:
        raise HTTPException(status_code=400, detail=f"{label}結束時間必須晚於開始時間")

    duration_hours = (end - start).total_seconds() / 3600
    if duration_hours > max_hours:
        raise HTTPException(status_code=400, detail=f"{label}時間長度不可超過 {max_hours} 小時")


def _validate_no_sleep_wake_overlap(
    user_id: UUID,
    start: datetime,
    end: datetime,
    record_type: str,
    db: Session,
):
    if record_type == "sleep":
        overlap = db.query(models.UsersTargetWakingPeriod).filter(
            models.UsersTargetWakingPeriod.user_id == user_id,
            models.UsersTargetWakingPeriod.is_active == True,
            models.UsersTargetWakingPeriod.deleted_at.is_(None),
            models.UsersTargetWakingPeriod.invalidated_at.is_(None),
            models.UsersTargetWakingPeriod.target_start_time < end,
            models.UsersTargetWakingPeriod.target_end_time > start,
        ).first()
    elif record_type == "wake":
        overlap = db.query(models.UsersRealSleepData).filter(
            models.UsersRealSleepData.user_id == user_id,
            models.UsersRealSleepData.is_active == True,
            models.UsersRealSleepData.deleted_at.is_(None),
            models.UsersRealSleepData.invalidated_at.is_(None),
            models.UsersRealSleepData.sleep_start_time < end,
            models.UsersRealSleepData.sleep_end_time > start,
        ).first()
    else:
        raise ValueError(f"Unsupported record type: {record_type}")

    if overlap:
        detail = overlap_detail(start, end, overlap.target_start_time, overlap.target_end_time, wake_id=overlap.id) if record_type == "sleep" else overlap_detail(overlap.sleep_start_time, overlap.sleep_end_time, start, end, sleep_id=overlap.id)
        raise HTTPException(
            status_code=400,
            detail=detail,
        )


def _validate_no_same_kind_overlap(user_id, start, end, kind, db, *, exclude_id=None):
    model = models.UsersRealSleepData if kind == "sleep" else models.UsersTargetWakingPeriod
    start_field = model.sleep_start_time if kind == "sleep" else model.target_start_time
    end_field = model.sleep_end_time if kind == "sleep" else model.target_end_time
    db.query(models.User).filter(models.User.user_id == user_id).with_for_update().first()
    active = db.query(model).filter(
        model.user_id == user_id, model.is_active.is_(True),
        model.deleted_at.is_(None), model.invalidated_at.is_(None),
    )
    if exclude_id is not None:
        if active.filter(model.id == exclude_id).populate_existing().first() is None:
            raise HTTPException(404, detail="record_already_inactive")
        active = active.filter(model.id != exclude_id)
    if active.filter(start_field < end, end_field > start).first() is not None:
        raise HTTPException(400, detail="periods_overlap")


def _validate_caffeine_intake(drink_name: str | None, caffeine_amount: int | None):
    if not drink_name or not drink_name.strip():
        raise HTTPException(status_code=422, detail="飲品名稱不可空白")
    if caffeine_amount is None:
        raise HTTPException(status_code=422, detail="咖啡因含量為必填")
    if caffeine_amount <= 0:
        raise HTTPException(status_code=400, detail="咖啡因含量必須大於 0 mg")
    if caffeine_amount > MAX_SINGLE_CAFFEINE_MG:
        raise HTTPException(
            status_code=400,
            detail=f"單筆咖啡因含量不可超過 {MAX_SINGLE_CAFFEINE_MG} mg，請確認是否誤填或拆成多筆紀錄",
        )


def _validate_pvt_result(data: schemas.UsersPVTResultsCreate):
    if not MIN_PVT_MEAN_RT_MS <= data.mean_rt <= MAX_PVT_MEAN_RT_MS:
        raise HTTPException(
            status_code=400,
            detail=f"清醒度測試平均反應時間需介於 {MIN_PVT_MEAN_RT_MS}-{MAX_PVT_MEAN_RT_MS} ms",
        )
    if data.lapses < 0 or data.false_starts < 0:
        raise HTTPException(status_code=400, detail="清醒度測試錯誤次數不可為負數")
    if data.lapses > MAX_PVT_EVENT_COUNT or data.false_starts > MAX_PVT_EVENT_COUNT:
        raise HTTPException(status_code=400, detail="清醒度測試錯誤次數異常，請重新確認")
    if data.kss_level is not None and not 1 <= data.kss_level <= 9:
        raise HTTPException(status_code=400, detail="KSS 睡意分數需介於 1-9")
    if not data.device or not data.device.strip():
        raise HTTPException(status_code=422, detail="測試裝置不可空白")


def _validate_recommendation_prerequisites(user_id: UUID, db: Session):
    has_sleep = db.query(models.UsersRealSleepData.id).filter(
        models.UsersRealSleepData.user_id == user_id,
        models.UsersRealSleepData.is_active == True,
        models.UsersRealSleepData.deleted_at.is_(None),
        models.UsersRealSleepData.invalidated_at.is_(None),
    ).first() is not None

    has_wake = db.query(models.UsersTargetWakingPeriod.id).filter(
        models.UsersTargetWakingPeriod.user_id == user_id,
        models.UsersTargetWakingPeriod.is_active == True,
        models.UsersTargetWakingPeriod.deleted_at.is_(None),
        models.UsersTargetWakingPeriod.invalidated_at.is_(None),
    ).first() is not None

    missing_fields = []
    if not has_sleep:
        missing_fields.append("實際睡眠週期")
    if not has_wake:
        missing_fields.append("目標清醒時段")

    if missing_fields:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "無法產生咖啡因推薦，請先補齊必要資料",
                "missing_fields": missing_fields,
                "notes": [
                    "咖啡因攝取紀錄可省略，省略時會視為沒有喝咖啡因。",
                    "清醒度測試可省略，省略時會使用預設個人化參數。",
                ],
            },
        )

    sleep_rows = db.query(models.UsersRealSleepData).filter(
        models.UsersRealSleepData.user_id == user_id,
        models.UsersRealSleepData.is_active == True,
        models.UsersRealSleepData.deleted_at.is_(None),
        models.UsersRealSleepData.invalidated_at.is_(None),
    ).all()
    wake_rows = db.query(models.UsersTargetWakingPeriod).filter(
        models.UsersTargetWakingPeriod.user_id == user_id,
        models.UsersTargetWakingPeriod.is_active == True,
        models.UsersTargetWakingPeriod.deleted_at.is_(None),
        models.UsersTargetWakingPeriod.invalidated_at.is_(None),
    ).all()

    conflict = first_recommendation_overlap(
        [(row.sleep_start_time, row.sleep_end_time) for row in sleep_rows],
        [(row.target_start_time, row.target_end_time) for row in wake_rows],
    )
    if conflict is not None:
        sleep, wake = sleep_rows[conflict[0]], wake_rows[conflict[1]]
        raise HTTPException(400, detail=overlap_detail(
            sleep.sleep_start_time, sleep.sleep_end_time, wake.target_start_time, wake.target_end_time,
            sleep_id=sleep.id, wake_id=wake.id,
        ))


def _invalidate_future_recommendations(user_id, db):
    db.query(RecommendationsCaffeine).filter(
        RecommendationsCaffeine.user_id == user_id,
        RecommendationsCaffeine.is_active.is_(True),
        RecommendationsCaffeine.recommended_caffeine_intake_timing >= datetime.now(timezone.utc),
    ).update({"is_active": False}, synchronize_session=False)
    db.commit()


# cors中間件
app.add_middleware(
    CORSMiddleware,
    allow_origins=[value.strip() for value in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if value.strip()],
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def private_responses(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-WakeMate-Validation"] = "system-audit-v4"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, error: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": list(item["loc"]), "msg": item["msg"], "type": item["type"]}
        for item in error.errors()
    ]})


# ------- 封裝共用觸發邏輯 -------
# 即時觸發計算
def trigger_calculation(triggered_by: UUID, *, force=False):
    conn = None
    try:
        with SessionLocal() as validation_db:
            try:
                _validate_recommendation_prerequisites(triggered_by, validation_db)
            except HTTPException as error:
                _invalidate_future_recommendations(triggered_by, validation_db)
                return {"status": "blocked", "detail": error.detail}
        conn = get_db_connection()
        # 批次跑，內部會自動判斷每個 user_id 是否需要更新
        recommendation_result = run_caffeine_recommendation(conn, only_user_id=triggered_by, force=force)
        alertness_result = run_alertness_data(conn, only_user_id=triggered_by, force=force)
        return {
            "status": "ok",
            "message": f"calculation batch finished (triggered by user {triggered_by})",
            "recommendation": recommendation_result,
            "alertness": alertness_result,
        }
    except Exception as e:
        return {"status": "error", "message": "calculation_failed"}
    finally:
        if conn is not None:
            conn.close()

# Scheduler 每小時補算
def scheduled_job():
    with SessionLocal() as session:
        cleanup_expired_sessions(session)
    conn = get_db_connection()
    try:
        run_caffeine_recommendation(conn)  # 全部使用者
        run_alertness_data(conn)
    finally:
        conn.close()


scheduler = BackgroundScheduler()
scheduler.add_job(scheduled_job, "interval", hours=1)
@app.on_event("startup")
def startup():
    models.Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        cleanup_expired_sessions(session)
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.execute(text("""ALTER TABLE alertness_data_for_visualization
                ADD COLUMN IF NOT EXISTS source_data_latest_at TIMESTAMPTZ"""))
    if os.getenv("RUN_BACKGROUND_JOBS", "true").lower() == "true" and not scheduler.running:
        scheduler.start()
atexit.register(lambda: scheduler.shutdown() if scheduler.running else None)

@app.head("/")
def head_root():
    return PlainTextResponse("ok")

@app.get("/ping")
def ping(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        return {"status": "success", "message": "連線成功"}
    except Exception as e:
        raise HTTPException(503, "database_unavailable")


@app.get("/questionnaire_links/", response_model=QuestionnaireLinksResponse)
def get_questionnaire_links():
    chinese_baseline_url = (
        os.getenv("QUESTIONNAIRE_ZH_BASELINE_URL", "").strip()
        or os.getenv("QUESTIONNAIRE_ZH_URL", "").strip()
    )
    indonesian_baseline_url = (
        os.getenv("QUESTIONNAIRE_ID_BASELINE_URL", "").strip()
        or os.getenv("QUESTIONNAIRE_ID_URL", "").strip()
    )

    return QuestionnaireLinksResponse(
        chinese_baseline_url=chinese_baseline_url,
        chinese_daily_url=os.getenv("QUESTIONNAIRE_ZH_DAILY_URL", "").strip(),
        indonesian_baseline_url=indonesian_baseline_url,
        indonesian_daily_url=os.getenv("QUESTIONNAIRE_ID_DAILY_URL", "").strip(),
        chinese_url=chinese_baseline_url,
        indonesian_url=indonesian_baseline_url,
        version=os.getenv("QUESTIONNAIRE_VERSION", "v1").strip() or "v1",
    )


# ========== API新增資料 ==========
@protected.post("/users_body_info/", response_model=schemas.UsersBodyInfoResponse)
def upsert_user_body_info(data: schemas.UsersBodyInfoCreate, db: Session = Depends(get_db)):
    try:
        stmt = insert(UsersBodyInfo).values(**data.model_dump())
        stmt = stmt.on_conflict_do_update(
            index_elements=[UsersBodyInfo.user_id],
            set_={
                "gender": stmt.excluded.gender,
                "age": stmt.excluded.age,
                "height": stmt.excluded.height,
                "weight": stmt.excluded.weight,
                "bmi": stmt.excluded.bmi,
                "updated_at": func.now(),
            },
        )
        db.execute(stmt)
        db.commit()

        # 重新查詢並回傳最新紀錄
        updated_info = db.query(UsersBodyInfo).filter(UsersBodyInfo.user_id == data.user_id).first()
        return updated_info
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="database_operation_failed")


@protected.post("/users_sleep/replace_day")
def replace_sleep_day(data: schemas.ReplaceDayRecords, db: Session = Depends(get_db)):
    result = replace_day_records(db, data, "sleep")
    result["calculation"] = trigger_calculation(data.user_id) if result["changed"] else {"status": "unchanged"}
    return result


@protected.post("/users_wake/replace_day")
def replace_wake_day(data: schemas.ReplaceDayRecords, db: Session = Depends(get_db)):
    result = replace_day_records(db, data, "wake")
    result["calculation"] = trigger_calculation(data.user_id) if result["changed"] else {"status": "unchanged"}
    return result


@protected.post("/users_sleep/", response_model=schemas.UsersRealSleepDataCreate_API_Response)
def create_user_sleep(data: schemas.UsersRealSleepDataCreate, db: Session = Depends(get_db)):
    _validate_time_range(
        data.sleep_start_time,
        data.sleep_end_time,
        "實際睡眠週期",
        MAX_SLEEP_DURATION_HOURS,
    )
    _validate_no_same_kind_overlap(data.user_id, data.sleep_start_time, data.sleep_end_time, "sleep", db)
    _validate_no_sleep_wake_overlap(
        data.user_id,
        data.sleep_start_time,
        data.sleep_end_time,
        "sleep",
        db,
    )
    try:
        now = datetime.now(timezone.utc)

        entry = models.UsersRealSleepData(
            user_id=data.user_id,
            sleep_start_time=data.sleep_start_time,
            sleep_end_time=data.sleep_end_time,
            created_at=now,
            updated_at=now,
            is_active=True,
            deleted_at=None,
            invalidated_at=None,
            edited_from_id=None
        )

        db.add(entry)
        db.commit()
        db.refresh(entry)

        calc_result = trigger_calculation(entry.user_id)

        return {
            "status": "success",
            "id": entry.id,
            "calculation": calc_result
        }

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="database_operation_failed")



@protected.post("/users_wake/", response_model=schemas.UsersTargetWakingPeriodCreate_API_Response)
def create_user_wake(data: schemas.UsersTargetWakingPeriodCreate, db: Session = Depends(get_db)):
    _validate_time_range(
        data.target_start_time,
        data.target_end_time,
        "目標清醒時段",
        MAX_WAKE_DURATION_HOURS,
    )
    _validate_no_same_kind_overlap(data.user_id, data.target_start_time, data.target_end_time, "wake", db)
    _validate_no_sleep_wake_overlap(
        data.user_id,
        data.target_start_time,
        data.target_end_time,
        "wake",
        db,
    )
    try:
        now = datetime.now(timezone.utc)

        entry = models.UsersTargetWakingPeriod(
            user_id=data.user_id,
            target_start_time=data.target_start_time,
            target_end_time=data.target_end_time,
            created_at=now,
            updated_at=now,
            is_active=True,
            deleted_at=None,
            invalidated_at=None,
            edited_from_id=None
        )

        db.add(entry)
        db.commit()
        db.refresh(entry)

        calc_result = trigger_calculation(entry.user_id)

        return {
            "status": "success",
            "id": entry.id,
            "calculation": calc_result
        }

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="database_operation_failed")


@protected.post("/users_intake/", response_model=schemas.UsersRealTimeIntakeCreate_API_Response)
def create_user_intake(data: schemas.UsersRealTimeIntakeCreate, db: Session = Depends(get_db)):
    _validate_caffeine_intake(data.drink_name, data.caffeine_amount)
    try:
        now = datetime.now(timezone.utc)

        entry = models.UsersRealTimeIntake(
            user_id=data.user_id,
            drink_name=data.drink_name,
            caffeine_amount=data.caffeine_amount,
            taking_timestamp=data.taking_timestamp,
            created_at=now,
            updated_at=now,
            is_active=True,
            deleted_at=None,
            invalidated_at=None,
            edited_from_id=None
        )

        db.add(entry)
        db.commit()
        db.refresh(entry)

        calc_result = trigger_calculation(entry.user_id)

        return {
            "status": "success",
            "id": entry.id,
            "calculation": calc_result
        }

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="database_operation_failed")

from core.personalize_params import update_user_params
@protected.post("/users_pvt/", response_model=schemas.UsersPVTResultsCreate_API_Response)
def create_user_pvt(data: schemas.UsersPVTResultsCreate, db: Session = Depends(get_db)):
    _validate_pvt_result(data)
    try:
        entry_id = save_pvt_result(db, data)

        # 觸發個人化參數更新
        conn = get_db_connection()
        try:
            params_update = update_user_params(conn, data.user_id)
        except Exception as e:
            conn.rollback()
            raise
        finally:
            conn.close()

        # 依然觸發批次計算
        calc_result = trigger_calculation(data.user_id)

        return {
            "status": "success",
            "id": entry_id,
            "params_update": params_update,  # 🔹 已包含 p0_value, kc, trait
            "calculation": calc_result
        }

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="database_operation_failed")


# ========== 取得資料 ==========
@app.get("/")
def read_root():
    return {"message": "API is running with APScheduler + real-time triggers"}


@protected.get("/users/", response_model=list[schemas.UserResponse])
def get_users(user_id: UUID = Depends(scoped_user_id), db: Session = Depends(get_db)):
    query = db.query(User)
    if user_id:
        query = query.filter(User.user_id == user_id)
    return query.all()


@protected.get("/login/", response_model=list[schemas.UserLoginResponse])
def get_users_login(user_id: UUID = Depends(scoped_user_id), db: Session = Depends(get_db)):
    query = db.query(User)
    if user_id:
        query = query.filter(User.user_id == user_id)
    return query.all()

@protected.get("/users_body_info/", response_model=list[schemas.UsersBodyInfoResponse])
def get_users_body_info(user_id: UUID = Depends(scoped_user_id), db: Session = Depends(get_db)):
    query = db.query(UsersBodyInfo)
    if user_id:
        query = query.filter(UsersBodyInfo.user_id == user_id)
    return query.all()


@protected.get("/users_sleep/", response_model=list[schemas.UsersRealSleepData_DB_Response])
def get_sleep_data(user_id: UUID = Depends(scoped_user_id), db: Session = Depends(get_db)):
    query = db.query(models.UsersRealSleepData).filter(
        models.UsersRealSleepData.is_active == True,
        models.UsersRealSleepData.deleted_at.is_(None),
        models.UsersRealSleepData.invalidated_at.is_(None)
    )

    if user_id:
        query = query.filter(models.UsersRealSleepData.user_id == user_id)

    query = query.order_by(models.UsersRealSleepData.sleep_start_time.desc())

    return query.all()


@protected.get("/users_wake/", response_model=list[schemas.UsersTargetWakingPeriod_DB_Response])
def get_wake_target(user_id: UUID = Depends(scoped_user_id), db: Session = Depends(get_db)):
    query = db.query(models.UsersTargetWakingPeriod).filter(
        models.UsersTargetWakingPeriod.is_active == True,
        models.UsersTargetWakingPeriod.deleted_at.is_(None),
        models.UsersTargetWakingPeriod.invalidated_at.is_(None)
    )

    if user_id:
        query = query.filter(models.UsersTargetWakingPeriod.user_id == user_id)

    query = query.order_by(models.UsersTargetWakingPeriod.target_start_time.desc())

    return query.all()


@protected.get("/users_intake/", response_model=list[schemas.UsersRealTimeIntake_DB_Response])
def get_intake_data(user_id: UUID = Depends(scoped_user_id), db: Session = Depends(get_db)):
    query = db.query(models.UsersRealTimeIntake).filter(
        models.UsersRealTimeIntake.is_active == True,
        models.UsersRealTimeIntake.deleted_at.is_(None),
        models.UsersRealTimeIntake.invalidated_at.is_(None)
    )

    if user_id:
        query = query.filter(models.UsersRealTimeIntake.user_id == user_id)

    query = query.order_by(models.UsersRealTimeIntake.taking_timestamp.desc())

    return query.all()

# PUT 和 DELETE: 修改和刪除 sleep, wake, intake ================================================================================================
@protected.put("/users_sleep/{entry_id}", response_model=schemas.UsersRealSleepDataCreate_API_Response)
def update_user_sleep(entry_id: int, data: schemas.UsersRealSleepDataUpdate, db: Session = Depends(get_db)):
    try:
        old_entry = db.query(models.UsersRealSleepData).filter(
            models.UsersRealSleepData.id == entry_id,
            models.UsersRealSleepData.is_active == True,
            models.UsersRealSleepData.deleted_at.is_(None),
            models.UsersRealSleepData.invalidated_at.is_(None)
        ).first()

        if not old_entry:
            raise HTTPException(status_code=404, detail="Sleep entry not found or already inactive")

        _validate_time_range(
            data.sleep_start_time,
            data.sleep_end_time,
            "實際睡眠週期",
            MAX_SLEEP_DURATION_HOURS,
        )
        _validate_no_same_kind_overlap(old_entry.user_id, data.sleep_start_time, data.sleep_end_time,
                                       "sleep", db, exclude_id=entry_id)
        _validate_no_sleep_wake_overlap(
            old_entry.user_id,
            data.sleep_start_time,
            data.sleep_end_time,
            "sleep",
            db,
        )

        now = datetime.now(timezone.utc)

        old_entry.is_active = False
        old_entry.invalidated_at = now
        old_entry.updated_at = now

        new_entry = models.UsersRealSleepData(
            user_id=old_entry.user_id,
            sleep_start_time=data.sleep_start_time,
            sleep_end_time=data.sleep_end_time,
            created_at=now,
            updated_at=now,
            is_active=True,
            deleted_at=None,
            invalidated_at=None,
            edited_from_id=old_entry.id
        )

        db.add(new_entry)
        db.commit()
        db.refresh(new_entry)

        calc_result = trigger_calculation(new_entry.user_id)

        return {
            "status": "success",
            "id": new_entry.id,
            "calculation": calc_result
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="database_operation_failed")

@protected.delete("/users_sleep/{entry_id}", response_model=schemas.UsersRealSleepDataCreate_API_Response)
def delete_user_sleep(entry_id: int, db: Session = Depends(get_db)):
    try:
        entry = db.query(models.UsersRealSleepData).filter(
            models.UsersRealSleepData.id == entry_id,
            models.UsersRealSleepData.is_active == True,
            models.UsersRealSleepData.deleted_at.is_(None),
            models.UsersRealSleepData.invalidated_at.is_(None)
        ).first()

        if not entry:
            raise HTTPException(status_code=404, detail="Sleep entry not found or already inactive")

        now = datetime.now(timezone.utc)

        entry.is_active = False
        entry.deleted_at = now
        entry.updated_at = now

        db.commit()

        calc_result = trigger_calculation(entry.user_id)

        return {
            "status": "success",
            "id": entry.id,
            "calculation": calc_result
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="database_operation_failed")
    
@protected.put("/users_wake/{entry_id}", response_model=schemas.UsersTargetWakingPeriodCreate_API_Response)
def update_user_wake(entry_id: int, data: schemas.UsersTargetWakingPeriodUpdate, db: Session = Depends(get_db)):
    try:
        old_entry = db.query(models.UsersTargetWakingPeriod).filter(
            models.UsersTargetWakingPeriod.id == entry_id,
            models.UsersTargetWakingPeriod.is_active == True,
            models.UsersTargetWakingPeriod.deleted_at.is_(None),
            models.UsersTargetWakingPeriod.invalidated_at.is_(None)
        ).first()

        if not old_entry:
            raise HTTPException(status_code=404, detail="Wake entry not found or already inactive")

        _validate_time_range(
            data.target_start_time,
            data.target_end_time,
            "目標清醒時段",
            MAX_WAKE_DURATION_HOURS,
        )
        _validate_no_same_kind_overlap(old_entry.user_id, data.target_start_time, data.target_end_time,
                                       "wake", db, exclude_id=entry_id)
        _validate_no_sleep_wake_overlap(
            old_entry.user_id,
            data.target_start_time,
            data.target_end_time,
            "wake",
            db,
        )

        now = datetime.now(timezone.utc)

        old_entry.is_active = False
        old_entry.invalidated_at = now
        old_entry.updated_at = now

        new_entry = models.UsersTargetWakingPeriod(
            user_id=old_entry.user_id,
            target_start_time=data.target_start_time,
            target_end_time=data.target_end_time,
            created_at=now,
            updated_at=now,
            is_active=True,
            deleted_at=None,
            invalidated_at=None,
            edited_from_id=old_entry.id
        )

        db.add(new_entry)
        db.commit()
        db.refresh(new_entry)

        calc_result = trigger_calculation(new_entry.user_id)

        return {
            "status": "success",
            "id": new_entry.id,
            "calculation": calc_result
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="database_operation_failed")

@protected.delete("/users_wake/{entry_id}", response_model=schemas.UsersTargetWakingPeriodCreate_API_Response)
def delete_user_wake(entry_id: int, db: Session = Depends(get_db)):
    try:
        entry = db.query(models.UsersTargetWakingPeriod).filter(
            models.UsersTargetWakingPeriod.id == entry_id,
            models.UsersTargetWakingPeriod.is_active == True,
            models.UsersTargetWakingPeriod.deleted_at.is_(None),
            models.UsersTargetWakingPeriod.invalidated_at.is_(None)
        ).first()

        if not entry:
            raise HTTPException(status_code=404, detail="Wake entry not found or already inactive")

        now = datetime.now(timezone.utc)

        entry.is_active = False
        entry.deleted_at = now
        entry.updated_at = now

        db.commit()

        calc_result = trigger_calculation(entry.user_id)

        return {
            "status": "success",
            "id": entry.id,
            "calculation": calc_result
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="database_operation_failed")

@protected.put("/users_intake/{entry_id}", response_model=schemas.UsersRealTimeIntakeCreate_API_Response)
def update_user_intake(entry_id: int, data: schemas.UsersRealTimeIntakeUpdate, db: Session = Depends(get_db)):
    try:
        old_entry = db.query(models.UsersRealTimeIntake).filter(
            models.UsersRealTimeIntake.id == entry_id,
            models.UsersRealTimeIntake.is_active == True,
            models.UsersRealTimeIntake.deleted_at.is_(None),
            models.UsersRealTimeIntake.invalidated_at.is_(None)
        ).first()

        if not old_entry:
            raise HTTPException(status_code=404, detail="Intake entry not found or already inactive")

        _validate_caffeine_intake(data.drink_name, data.caffeine_amount)

        now = datetime.now(timezone.utc)

        old_entry.is_active = False
        old_entry.invalidated_at = now
        old_entry.updated_at = now

        new_entry = models.UsersRealTimeIntake(
            user_id=old_entry.user_id,
            drink_name=data.drink_name,
            caffeine_amount=data.caffeine_amount,
            taking_timestamp=data.taking_timestamp,
            created_at=now,
            updated_at=now,
            is_active=True,
            deleted_at=None,
            invalidated_at=None,
            edited_from_id=old_entry.id
        )

        db.add(new_entry)
        db.commit()
        db.refresh(new_entry)

        calc_result = trigger_calculation(new_entry.user_id)

        return {
            "status": "success",
            "id": new_entry.id,
            "calculation": calc_result
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="database_operation_failed")

@protected.delete("/users_intake/{entry_id}", response_model=schemas.UsersRealTimeIntakeCreate_API_Response)
def delete_user_intake(entry_id: int, db: Session = Depends(get_db)):
    try:
        entry = db.query(models.UsersRealTimeIntake).filter(
            models.UsersRealTimeIntake.id == entry_id,
            models.UsersRealTimeIntake.is_active == True,
            models.UsersRealTimeIntake.deleted_at.is_(None),
            models.UsersRealTimeIntake.invalidated_at.is_(None)
        ).first()

        if not entry:
            raise HTTPException(status_code=404, detail="Intake entry not found or already inactive")

        now = datetime.now(timezone.utc)

        entry.is_active = False
        entry.deleted_at = now
        entry.updated_at = now

        db.commit()

        calc_result = trigger_calculation(entry.user_id)

        return {
            "status": "success",
            "id": entry.id,
            "calculation": calc_result
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="database_operation_failed")
# ================================================================================================

@protected.get("/users_pvt/", response_model=list[schemas.UsersPVTResults_DB_Response])
def get_pvt_results(user_id: UUID = Depends(scoped_user_id), db: Session = Depends(get_db)):
    query = db.query(UsersPVTResults)
    if user_id:
        query = query.filter(UsersPVTResults.user_id == user_id)
    return query.all()


@protected.get("/recommendations/", response_model=list[schemas.RecommendationsCaffeine_DB_Response])
def get_recommendations(user_id: UUID = Depends(scoped_user_id), db: Session = Depends(get_db)):
    _validate_recommendation_prerequisites(user_id, db)
    latest_source = (
        db.query(func.max(RecommendationsCaffeine.source_data_latest_at))
        .filter(RecommendationsCaffeine.user_id == user_id)
        .filter(RecommendationsCaffeine.is_active == True)
        .scalar_subquery()
    )
    return (
        db.query(RecommendationsCaffeine)
        .filter(RecommendationsCaffeine.user_id == user_id)
        .filter(RecommendationsCaffeine.is_active == True)
        .filter(RecommendationsCaffeine.source_data_latest_at == latest_source)
        .order_by(RecommendationsCaffeine.recommended_caffeine_intake_timing.asc())
        .all()
    )


@protected.post("/calculate/")
def calculate_recommendations(user_id: UUID = Depends(scoped_user_id), db: Session = Depends(get_db)):
    try:
        _validate_recommendation_prerequisites(user_id, db)
    except HTTPException:
        _invalidate_future_recommendations(user_id, db)
        raise
    result = trigger_calculation(user_id, force=True)
    if result["status"] == "blocked":
        raise HTTPException(400, detail=result["detail"])
    if result["status"] != "ok":
        raise HTTPException(status_code=500, detail=result["message"])
    return result


@protected.get("/alertness_data/", response_model=list[schemas.AlertnessData_DB_Response])
def get_alertness_data(user_id: UUID = Depends(scoped_user_id), db: Session = Depends(get_db)):
    query = db.query(AlertnessDataForVisualization)
    if user_id:
        query = query.filter(AlertnessDataForVisualization.user_id == user_id)
    return query.all()


# ====================== 以下是 DEVICE 的資料批量傳送端口====================================

# ================== 批量寫入 Heart Rate ==================
@protected.post("/device/heart_rate/bulk", response_model=list[schemas.DeviceHeartRate_DB_Response])
def create_heart_rate_bulk(payload: schemas.BulkHeartRate, db: Session = Depends(get_db)):
    objs = [models.DeviceHeartRateData(**record.model_dump()) for record in payload.records]
    db.add_all(objs)
    db.commit()
    for obj in objs:
        db.refresh(obj)
    return objs


@protected.get("/device/heart_rate", response_model=list[schemas.DeviceHeartRate_DB_Response])
def get_heart_rate(user_id: UUID = Depends(scoped_user_id), db: Session = Depends(get_db)):
    query = db.query(models.DeviceHeartRateData)
    if user_id:
        query = query.filter(models.DeviceHeartRateData.user_id == user_id)
    return query.all()


# ================== 批量寫入 XYZ Time ==================
@protected.post("/device/xyz_time/bulk", response_model=list[schemas.DeviceXYZTime_DB_Response])
def create_xyz_time_bulk(payload: schemas.BulkXYZTime, db: Session = Depends(get_db)):
    try:
        objs = [models.DeviceXYZTimeData(**record.model_dump()) for record in payload.records]
        db.add_all(objs)
        db.commit()
        for obj in objs:
            db.refresh(obj)
        return objs
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="database_operation_failed")


@protected.get("/device/xyz_time", response_model=list[schemas.DeviceXYZTime_DB_Response])
def get_xyz_time(user_id: UUID = Depends(scoped_user_id), db: Session = Depends(get_db)):
    query = db.query(models.DeviceXYZTimeData)
    if user_id:
        query = query.filter(models.DeviceXYZTimeData.user_id == user_id)
    return query.all()


app.include_router(protected)

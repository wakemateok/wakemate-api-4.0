# app/main.py
from fastapi import FastAPI, HTTPException, Depends, Query, status
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
from passlib.context import CryptContext
from .database import engine, SessionLocal
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler
import atexit
from fastapi.responses import PlainTextResponse

# 計算模組
from core.caffeine_recommendation import run_caffeine_recommendation
from core.alertness_data import run_alertness_data
from core.database import get_db_connection

models.Base.metadata.create_all(bind=engine)
app = FastAPI()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# 依賴注入：取得 DB session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# cors中間件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------- 封裝共用觸發邏輯 -------
# 即時觸發計算
def trigger_calculation(triggered_by: UUID):
    try:
        conn = get_db_connection()
        # 批次跑，內部會自動判斷每個 user_id 是否需要更新
        run_caffeine_recommendation(conn)
        run_alertness_data(conn)
        conn.close()
        return {
            "status": "ok",
            "message": f"calculation batch finished (triggered by user {triggered_by})"
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

# Scheduler 每小時補算
def scheduled_job():
    conn = get_db_connection()
    try:
        run_caffeine_recommendation(conn)  # 全部使用者
        run_alertness_data(conn)
    finally:
        conn.close()


scheduler = BackgroundScheduler()
scheduler.add_job(scheduled_job, "interval", hours=1)
scheduler.start()
atexit.register(lambda: scheduler.shutdown())

@app.head("/")
def head_root():
    return PlainTextResponse("ok")

@app.get("/ping")
def ping(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        return {"status": "success", "message": "連線成功"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ========== API新增資料 ==========
@app.post("/users/", response_model=schemas.UserResponse)
def register_user(user: schemas.UserCreate, db: Session = Depends(get_db)):
    db_user = db.query(models.User).filter(models.User.email == user.email).first()
    if db_user:
        raise HTTPException(status_code=400, detail="Email already registered")

    hashed_password = pwd_context.hash(user.password)
    new_user = models.User(
        user_id=uuid4(),
        email=user.email,
        hashed_password=hashed_password,
        name=user.name
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    # 建立 users_params
    # 預設參數 (dict 格式)
    default_values = {
        "m_c": 1.0,
        "k_a": 1.25,
        "k_c": 0.20,
        "trait_alertness": 0.0,
        "p0_value": 270.0,
        "pvt_count_7d": 0
    }

    # 確保 users_params 有正確的預設值
    existing_params = db.query(models.UsersParams).filter_by(user_id=new_user.user_id).first()
    if not existing_params:
        db.add(models.UsersParams(user_id=new_user.user_id, **default_values))
    else:
        for k, v in default_values.items():
            setattr(existing_params, k, v)

    db.commit()


    return schemas.UserResponse.from_orm(new_user)



@app.post("/login/", response_model=schemas.UserLoginResponse)
def login_user(user: UserLogin, db: Session = Depends(get_db)):
    db_user = db.query(User).filter(User.email == user.email).first()
    if not db_user or not pwd_context.verify(user.password, db_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password"
        )

    # 更新最後登入時間
    db_user.last_login = func.now()
    db.commit()
    db.refresh(db_user)

    return db_user


@app.post("/users_body_info/", response_model=schemas.UsersBodyInfoResponse)
def upsert_user_body_info(data: schemas.UsersBodyInfoCreate, db: Session = Depends(get_db)):
    try:
        stmt = insert(UsersBodyInfo).values(**data.dict())
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
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/users_sleep/", response_model=schemas.UsersRealSleepDataCreate_API_Response)
def create_user_sleep(data: schemas.UsersRealSleepDataCreate, db: Session = Depends(get_db)):
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
        raise HTTPException(status_code=400, detail=str(e))



@app.post("/users_wake/", response_model=schemas.UsersTargetWakingPeriodCreate_API_Response)
def create_user_wake(data: schemas.UsersTargetWakingPeriodCreate, db: Session = Depends(get_db)):
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
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/users_intake/", response_model=schemas.UsersRealTimeIntakeCreate_API_Response)
def create_user_intake(data: schemas.UsersRealTimeIntakeCreate, db: Session = Depends(get_db)):
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
        raise HTTPException(status_code=400, detail=str(e))

from core.personalize_params import update_user_params
@app.post("/users_pvt/", response_model=schemas.UsersPVTResultsCreate_API_Response)
def create_user_pvt(data: schemas.UsersPVTResultsCreate, db: Session = Depends(get_db)):
    try:
        entry = models.UsersPVTResults(**data.dict())
        db.add(entry)
        db.commit()
        db.refresh(entry)

        # 觸發個人化參數更新
        conn = get_db_connection()
        try:
            params_update = update_user_params(conn, entry.user_id)
        except Exception as e:
            conn.rollback()
            raise
        finally:
            conn.close()

        # 依然觸發批次計算
        calc_result = trigger_calculation(entry.user_id)

        return {
            "status": "success",
            "id": entry.id,
            "params_update": params_update,  # 🔹 已包含 p0_value, kc, trait
            "calculation": calc_result
        }

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


# ========== 取得資料 ==========
@app.get("/")
def read_root():
    return {"message": "API is running with APScheduler + real-time triggers"}


@app.get("/users/")
def get_users(user_id: UUID = Query(None), db: Session = Depends(get_db)):
    query = db.query(User)
    if user_id:
        query = query.filter(User.user_id == user_id)
    return query.all()


@app.get("/login/")
def get_users_login(user_id: UUID = Query(None), db: Session = Depends(get_db)):
    query = db.query(User)
    if user_id:
        query = query.filter(User.user_id == user_id)
    return query.all()

@app.get("/users_body_info/", response_model=list[schemas.UsersBodyInfoResponse])
def get_users_body_info(user_id: UUID = Query(None), db: Session = Depends(get_db)):
    query = db.query(UsersBodyInfo)
    if user_id:
        query = query.filter(UsersBodyInfo.user_id == user_id)
    return query.all()


@app.get("/users_sleep/", response_model=list[schemas.UsersRealSleepData_DB_Response])
def get_sleep_data(user_id: UUID = Query(None), db: Session = Depends(get_db)):
    query = db.query(models.UsersRealSleepData).filter(
        models.UsersRealSleepData.is_active == True,
        models.UsersRealSleepData.deleted_at.is_(None),
        models.UsersRealSleepData.invalidated_at.is_(None)
    )

    if user_id:
        query = query.filter(models.UsersRealSleepData.user_id == user_id)

    query = query.order_by(models.UsersRealSleepData.sleep_start_time.desc())

    return query.all()


@app.get("/users_wake/", response_model=list[schemas.UsersTargetWakingPeriod_DB_Response])
def get_wake_target(user_id: UUID = Query(None), db: Session = Depends(get_db)):
    query = db.query(models.UsersTargetWakingPeriod).filter(
        models.UsersTargetWakingPeriod.is_active == True,
        models.UsersTargetWakingPeriod.deleted_at.is_(None),
        models.UsersTargetWakingPeriod.invalidated_at.is_(None)
    )

    if user_id:
        query = query.filter(models.UsersTargetWakingPeriod.user_id == user_id)

    query = query.order_by(models.UsersTargetWakingPeriod.target_start_time.desc())

    return query.all()


@app.get("/users_intake/", response_model=list[schemas.UsersRealTimeIntake_DB_Response])
def get_intake_data(user_id: UUID = Query(None), db: Session = Depends(get_db)):
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
@app.put("/users_sleep/{entry_id}", response_model=schemas.UsersRealSleepDataCreate_API_Response)
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
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/users_sleep/{entry_id}", response_model=schemas.UsersRealSleepDataCreate_API_Response)
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
        raise HTTPException(status_code=400, detail=str(e))
    
@app.put("/users_wake/{entry_id}", response_model=schemas.UsersTargetWakingPeriodCreate_API_Response)
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
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/users_wake/{entry_id}", response_model=schemas.UsersTargetWakingPeriodCreate_API_Response)
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
        raise HTTPException(status_code=400, detail=str(e))

@app.put("/users_intake/{entry_id}", response_model=schemas.UsersRealTimeIntakeCreate_API_Response)
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
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/users_intake/{entry_id}", response_model=schemas.UsersRealTimeIntakeCreate_API_Response)
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
        raise HTTPException(status_code=400, detail=str(e))            
# ================================================================================================

@app.get("/users_pvt/", response_model=list[schemas.UsersPVTResults_DB_Response])
def get_pvt_results(user_id: UUID = Query(None), db: Session = Depends(get_db)):
    query = db.query(UsersPVTResults)
    if user_id:
        query = query.filter(UsersPVTResults.user_id == user_id)
    return query.all()


@app.get("/recommendations/", response_model=list[schemas.RecommendationsCaffeine_DB_Response])
def get_recommendations(user_id: UUID = Query(...), db: Session = Depends(get_db)):
    return (
        db.query(RecommendationsCaffeine)
        .filter(RecommendationsCaffeine.user_id == user_id)
        .filter(RecommendationsCaffeine.is_active == True)
        .order_by(RecommendationsCaffeine.recommended_caffeine_intake_timing.asc())
        .all()
    )


@app.get("/alertness_data/", response_model=list[schemas.AlertnessData_DB_Response])
def get_alertness_data(user_id: UUID = Query(None), db: Session = Depends(get_db)):
    query = db.query(AlertnessDataForVisualization)
    if user_id:
        query = query.filter(AlertnessDataForVisualization.user_id == user_id)
    return query.all()


# ====================== 以下是 DEVICE 的資料批量傳送端口====================================

# ================== 批量寫入 Heart Rate ==================
@app.post("/device/heart_rate/bulk", response_model=list[schemas.DeviceHeartRate_DB_Response])
def create_heart_rate_bulk(payload: schemas.BulkHeartRate, db: Session = Depends(get_db)):
    objs = [models.DeviceHeartRate(**record.dict()) for record in payload.records]
    db.add_all(objs)
    db.commit()
    for obj in objs:
        db.refresh(obj)
    return objs


@app.get("/device/heart_rate", response_model=list[schemas.DeviceHeartRate_DB_Response])
def get_heart_rate(user_id: UUID = Query(None), db: Session = Depends(get_db)):
    query = db.query(models.DeviceHeartRateData)
    if user_id:
        query = query.filter(models.DeviceHeartRateData.user_id == user_id)
    return query.all()


# ================== 批量寫入 XYZ Time ==================
@app.post("/device/xyz_time/bulk", response_model=list[schemas.DeviceXYZTime_DB_Response])
def create_xyz_time_bulk(payload: schemas.BulkXYZTime, db: Session = Depends(get_db)):
    try:
        objs = [models.DeviceXYZTimeData(**record.dict()) for record in payload.records]
        db.add_all(objs)
        db.commit()
        for obj in objs:
            db.refresh(obj)
        return objs
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"寫入失敗: {str(e)}")


@app.get("/device/xyz_time", response_model=list[schemas.DeviceXYZTime_DB_Response])
def get_xyz_time(user_id: UUID = Query(None), db: Session = Depends(get_db)):
    query = db.query(models.DeviceXYZTimeData)
    if user_id:
        query = query.filter(models.DeviceXYZTimeData.user_id == user_id)
    return query.all()
from flask import Flask, render_template, request, flash, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash
from collections import Counter
from datetime import datetime
import json
import os
import logging

app = Flask(__name__)
app.secret_key = os.urandom(24)
app.config['SQLALCHEMY_DATABASE_URI'] = "sqlite:///users.db"
db = SQLAlchemy(app)


#DB for login
class User(db.Model):
    id = db.Column(db.Integer, primary_key = True)
    email = db.Column(db.String(200), nullable = False, unique=True)
    password = db.Column(db.String(200), nullable = False)


#DB for Alert
class Alert(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    asset_id = db.Column(db.String(100))
    alert_type = db.Column(db.String(50))  # cpu, memory, storage, etc.
    severity = db.Column(db.String(20))  # warning, critical
    message = db.Column(db.String(255))
    status = db.Column(db.String(20))  # active or resolved
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)


#create DB file
with app.app_context():
    db.create_all()


@app.route("/")
def home():
    return render_template("login.html")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]
        confirm_password = request.form["confirm_password"]

        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return redirect(url_for("signup"))

        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            flash("User with this email already exists. Please log in.", "error")
            return redirect(url_for("home"))
        
        hashed_pw = generate_password_hash(password)
        
        new_user = User(email=email, password=hashed_pw)
        db.session.add(new_user)
        db.session.commit()
        flash("Signup successful. Please log in.", "success")
        return redirect(url_for("home")) 
    
    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password, password):
            session.permanent = True
            session["user"] = user.email
            return redirect(url_for("dashboard"))
        else:
            flash("Invalid credentials. Try again.", "error")
            return redirect(url_for("home"))
    
    return render_template("login.html")


def get_storage_color(usage):
    if usage <= 50:
        return "#22c55e"
    elif usage >50 and usage < 85:
        return "#eab308"
    else:
        return "#ef4444"


def evaluate_status(value, thresholds):
    if value <= thresholds['ok']:
        return 'ok'
    elif value <= thresholds['warning']:
        return 'warning'
    return 'critical'


def get_overall_health(asset):
    statuses = []
    status_details = []
    
    # 1. CPU
    cpu = asset.get("cpu_usage_percent", 0)
    cpu_status = evaluate_status(cpu, {"ok": 50, "warning": 80})
    statuses.append(cpu_status)
    status_details.append({
        "type": "CPU",
        "value": cpu,
        "status": cpu_status,
        "thresholds": {"ok": 50, "warning": 80}
    })

    # 2. Memory
    memory = asset.get("memory_usage_percent", 0)
    memory_status = evaluate_status(memory, {"ok": 70, "warning": 95})
    statuses.append(memory_status)
    status_details.append({
        "type": "Memory",
        "value": memory,
        "status": memory_status,
        "thresholds": {"ok": 70, "warning": 95}
    })

    # 3. Temperature
    temp = asset.get("temp", 0)
    temp_status = evaluate_status(temp, {"ok": 40, "warning": 50})
    statuses.append(temp_status)
    status_details.append({
        "type": "Temperature",
        "value": temp,
        "status": temp_status,
        "thresholds": {"ok": 40, "warning": 50}
    })

    # 4. Storage
    try:
        used = float(asset.get("storage_used", 0))
        total = float(asset.get("storage_total", 0))
        storage_pct = (used / total) * 100 if total > 0 else 0
    except (ValueError, ZeroDivisionError):
        storage_pct = 0
    asset["storage_usage_percent"] = int(round(storage_pct, 1))
    storage_status = evaluate_status(storage_pct, {"ok": 80, "warning": 90})
    statuses.append(storage_status)
    status_details.append({
        "type": "Storage",
        "value": int(round(storage_pct, 1)),
        "status": storage_status,
        "thresholds": {"ok": 80, "warning": 90}
    })

    # 5. Contract Expiry
    try:
        expiry_str = asset.get("contract_expiry", "")
        expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d")
        days_remaining = (expiry_date - datetime.today()).days
        if days_remaining > 180:
            contract_status = 'ok'
        elif days_remaining > 30:
            contract_status = 'warning'
        else:
            contract_status = 'critical'
        statuses.append(contract_status)
        status_details.append({
            "type": "Contract",
            "value": days_remaining,
            "status": contract_status,
            "expiry_date": expiry_str
        })
    except:
        statuses.append('warning')
        status_details.append({
            "type": "Contract",
            "value": -1,
            "status": 'warning',
            "expiry_date": expiry_str
        })
    
    # Determine overall health
    if "critical" in statuses:
        overall_health = "Critical"
    elif "warning" in statuses:
        overall_health = "Warning"
    else:
        overall_health = "OK"
    
    return overall_health, status_details


def track_alerts(asset, status_details):
    asset_id = asset.get("asset_id", "Unknown")

    for detail in status_details:
        alert_type = detail["type"]
        value = detail["value"]
        severity = detail["status"]

        # Skip invalid types
        if alert_type not in ["CPU", "Memory", "Temperature", "Storage", "Contract"]:
            continue

        # Build message
        if alert_type == "Contract":
            if value >= 0:
                message = f"Contract expires in {value} days"
            else:
                message = "Contract Expired"
        else:
            message = f"{alert_type} usage at {value}%"

        # Find the most recent alert for this asset and type
        existing_alert = Alert.query.filter_by(
            asset_id=asset_id,
            alert_type=alert_type
        ).order_by(Alert.updated_at.desc()).first()

        if severity == "ok":
            # resolve active alert if it exists
            if existing_alert and existing_alert.status == "active":
                existing_alert.status = "resolved"
                existing_alert.updated_at = datetime.now()
                db.session.commit()
        else:
            # warning or critical
            if existing_alert:
                if existing_alert.status == "resolved":
                    # Create a NEW alert instead of reactivating the old one
                    new_alert = Alert(
                        asset_id=asset_id,
                        alert_type=alert_type,
                        severity=severity,
                        message=message,
                        status="active",
                    )
                    db.session.add(new_alert)
                    db.session.commit()
                else:
                    # Active alert exists - update if changed
                    if existing_alert.severity != severity or existing_alert.message != message:
                        existing_alert.severity = severity
                        existing_alert.message = message
                        existing_alert.updated_at = datetime.now()
                        db.session.commit()
            else:
                # No previous alert - create new
                new_alert = Alert(
                    asset_id=asset_id,
                    alert_type=alert_type,
                    severity=severity,
                    message=message,
                    status="active"
                )
                db.session.add(new_alert)
                db.session.commit()


def load_json():
    assets = []
    try:
        with open("static/assets.json") as f:
            content = f.read().strip()
            if content:
                data = json.loads(content)
                assets = data.get("servers_details", [])
    except (json.JSONDecodeError, FileNotFoundError) as e:
        logging.warning(f"Error loading assets.json: {e}")
    
    return assets


def load_assets_and_track_alerts():
    assets = load_json()
    for asset in assets:
        overall_health, status_details = get_overall_health(asset)
        asset["health_status"] = overall_health
        track_alerts(asset, status_details)
    return assets


@app.route("/dashboard")
def dashboard():
    if "user" in session:
        user = session["user"]
        assets = load_json()
        total_assets = len(assets)
        
        asset_type_counter = Counter()
        
        for asset in assets:
            overall_health, status_details = get_overall_health(asset)
            asset["health_status"] = overall_health
            track_alerts(asset, status_details)
            
            raw_type = asset.get("type", "").strip().capitalize()

            if raw_type in ["Server", "Storage"]:
                asset_type_counter[raw_type] += 1
            else:
                asset_type_counter["Other"] += 1

        healthy_assets = sum(1 for a in assets if a["health_status"].lower() == "ok")
        warning_assets = sum(1 for a in assets if a["health_status"].lower() == "warning")
        critical_assets = sum(1 for a in assets if a["health_status"].lower() == "critical")
        last_updated = datetime.now().strftime("%B %d, %Y at %I:%M %p")

        asset_type_counts = [
            asset_type_counter.get("Server", 0),
            asset_type_counter.get("Storage", 0),
            asset_type_counter.get("Other", 0)
        ]
        
        return render_template(
            "dashboard.html",
            assets=assets,
            user=user,
            healthy_assets=healthy_assets,
            warning_assets=warning_assets,
            critical_assets=critical_assets,
            total_assets=total_assets,
            last_updated=last_updated,
            asset_type_counts=asset_type_counts
        )
    else:
        return redirect(url_for("home"))


@app.route("/assets")
def assets():
    if "user" in session:
        user = session["user"]
        assets = load_json()
        
        # Get storage color and percentage
        for asset in assets:
            overall_health, status_details = get_overall_health(asset)
            asset["health_status"] = overall_health
            track_alerts(asset, status_details)
            
            usage_mem = asset.get("memory_usage_percent", 0)
            asset["memory_color"] = get_storage_color(usage_mem)

            usage_cpu = asset.get("cpu_usage_percent", 0)
            asset["cpu_color"] = get_storage_color(usage_cpu)
            
            stor_used = asset.get("storage_used", 0)
            stor_total = asset.get("storage_total", 0)
            try:
                # Convert to float and calculate percentage
                storage_used = float(stor_used)
                storage_total = float(stor_total)
                if storage_total > 0:
                    storage_pct = (storage_used / storage_total) * 100
                else:
                    storage_pct = 0
            except ValueError:
                storage_pct = 0

            asset["storage_usage_percent"] = int(round(storage_pct, 1))
            asset["storage_color"] = get_storage_color(int(round(storage_pct, 1)))

        # Current time for "Last updated"
        last_updated = datetime.now().strftime("%B %d, %Y at %I:%M %p")
        
        return render_template(
            "assets.html",
            assets=assets,
            user=user,
            get_storage_color=get_storage_color,
            last_updated=last_updated)
    else:
        return redirect(url_for("home"))


@app.route("/alerts")
def alerts():
    if "user" in session:
        user = session["user"]
        load_assets_and_track_alerts()
        
        active_alerts = Alert.query.order_by(Alert.updated_at.desc()).all()
        last_updated = datetime.now().strftime("%B %d, %Y at %I:%M %p")
        return render_template("alerts.html", alerts=active_alerts, last_updated=last_updated, user=user,)
    else:
        return redirect(url_for("home"))

@app.route("/logout")
def logout():
    session.pop("user", None)
    flash("Logged out successfully.", "logout")
    return redirect(url_for("home"))


if __name__ == "__main__":
    app.run(debug=True)

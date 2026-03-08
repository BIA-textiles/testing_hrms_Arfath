
import os, functools
from datetime import datetime
from bson import ObjectId
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort, make_response
from pymongo import MongoClient, ASCENDING, DESCENDING, errors
from werkzeug.security import generate_password_hash, check_password_hash
from flask.json.provider import DefaultJSONProvider

class MongoJSONProvider(DefaultJSONProvider):
    def default(self, o):
        if isinstance(o, ObjectId):
            return str(o)
        if isinstance(o, datetime):
            return o.isoformat()
        return super().default(o)

load_dotenv()

def create_app():
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.json = MongoJSONProvider(app)
    app.secret_key = os.environ.get("SECRET_KEY")
    app.jinja_env.autoescape = True
    # allow built-in casting if templates ever use it
    app.jinja_env.globals.update(str=str)

    client = MongoClient(os.environ.get("MONGODB_URI"))
    db = client[os.environ.get("MONGO_DB_NAME")]
    app.db = db

    # Indexes
    try:
        db.users.create_index([("id", ASCENDING)], unique=True)
        db.users.create_index([("email", ASCENDING)], unique=False)
        for coll in [
            "documents","leave_requests","visa_applications","flight_requests",
            "insurance_applications","cab_requests","visas"
        ]:
            db[coll].create_index([("employee_id", ASCENDING)])
            db[coll].create_index([("status", ASCENDING)])
            db[coll].create_index([("created_at", ASCENDING)])
    except errors.PyMongoError as e:
        print("Index warning:", e)

    # One-time migration: Lowercase all emails, IDs, and Supervisor IDs
    try:
        all_users = list(db.users.find())
        for user in all_users:
            update_fields = {}
            if user.get("email"):
                update_fields["email"] = user["email"].lower()
            if user.get("id"):
                update_fields["id"] = user["id"].lower()
            if user.get("supervisor"):
                update_fields["supervisor"] = user["supervisor"].lower()
            
            # Ensure is_online is initialized
            if user.get("is_online") is None:
                update_fields["is_online"] = False
            
            if update_fields:
                db.users.update_one({"_id": user["_id"]}, {"$set": update_fields})
    except Exception as e:
        print("Migration error:", e)

    # Helpers
    def login_required(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            if "user_id" not in session:
                nxt = request.full_path if request.query_string else request.path
                return redirect(url_for("login", next=nxt))
            return view(*args, **kwargs)
        return wrapper

    def current_user():
        uid = session.get("user_id")
        if not uid:
            return None
        return db.users.find_one({"_id": ObjectId(uid)})

    def ensure_role(*roles):
        def deco(view):
            @functools.wraps(view)
            def wrapper(*args, **kwargs):
                user = current_user()
                if not user or user.get("role") not in roles:
                    abort(403)
                return view(*args, **kwargs)
            return wrapper
        return deco

    # Seed demo users if none
    if db.users.estimated_document_count() == 0:
        demo = [
            {"id":"emp001","name":"John Doe","email":"john.doe@company.com","role":"employee","department":"IT","designation":"Software Engineer","grade":5,"band":"C","password_hash":generate_password_hash("password"),"leave_balance":14,"supervisor":"super001"},
            {"id":"super001","name":"Manager Smith","email":"manager@company.com","role":"supervisor","department":"IT","designation":"IT Manager","grade":10,"band":"B","password_hash":generate_password_hash("password"),"leave_balance":20},
            {"id":"hr001","name":"HR Admin","email":"hr.admin@company.com","role":"hr","department":"Human Resources","designation":"HR Manager","grade":12,"band":"A","password_hash":generate_password_hash("password"),"leave_balance":18},
            {"id":"it001","name":"IT Admin","email":"it.admin@company.com","role":"itadmin","department":"IT","designation":"IT Administrator","grade":9,"band":"B","password_hash":generate_password_hash("password"),"leave_balance":18}
        ]
        db.users.insert_many(demo)

    # Public
    @app.get("/")
    def root():
        if "user_id" in session:
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET","POST"])
    def login():
        if request.method == "POST":
            login_key = (request.form.get("login") or "").strip()
            password = request.form.get("password") or ""
            # Try exact match first, then lowercase for both id and email
            login_key_lower = login_key.lower()
            user = db.users.find_one({"$or":[
                {"id": login_key},
                {"id": login_key_lower},
                {"email": login_key_lower}
            ]})
            if not user or not check_password_hash(user["password_hash"], password):
                flash("Invalid Employee ID/Email or Password.", "danger")
                return redirect(url_for("login"))
            db.users.update_one({"_id": user["_id"]}, {"$set": {"is_online": True}})
            session.update({
                "user_id": str(user["_id"]),
                "id": user["id"],   # store the canonical id from DB
                "name": user["name"],
                "role": user["role"]
            })
            return redirect(request.args.get("next") or url_for("dashboard"))
        return render_template("auth/login.html")

    @app.get("/logout")
    def logout():
        uid = session.get("user_id")
        if uid:
            db.users.update_one({"_id": ObjectId(uid)}, {"$set": {"is_online": False}})
        session.clear()
        return redirect(url_for("login"))

    # Dashboard redirect by role
    @app.get("/dashboard")
    @login_required
    def dashboard():
        return redirect(url_for("employee_my_info"))

    @app.context_processor
    def inject_pending_counts():
        if "user_id" not in session:
            return dict(pending_counts={"total": 0, "categories": {}})
        
        role = session.get("role")
        user_id = session.get("id")
        counts = {"total": 0, "categories": {}}
        
        colls = {
            "leave": "leave_requests",
            "visa": "visa_applications",
            "flight": "flight_requests",
            "cab": "cab_requests",
            "insurance": "insurance_applications",
            "documents": "documents"
        }
        
        if role == "supervisor":
            team_ids = [u["id"] for u in db.users.find({"supervisor": user_id}, {"id": 1})]
            # Supervisors only approve requests (not documents)
            supervisor_colls = {k: v for k, v in colls.items() if k != "documents"}
            for cat, coll in supervisor_colls.items():
                c = db[coll].count_documents({"employee_id": {"$in": team_ids}, "status": "Pending"})
                counts["categories"][cat] = c
                counts["total"] += c
            counts["categories"]["documents"] = 0
        elif role in ["hr", "itadmin"]:
            for cat, coll in colls.items():
                if cat == "documents":
                    c = db[coll].count_documents({"status": {"$in": ["Pending", "SupervisorApproved"]}})
                else:
                    c = db[coll].count_documents({"status": "SupervisorApproved"})
                counts["categories"][cat] = c
                counts["total"] += c
                
        return dict(pending_counts=counts)

    # ---------------- Employee pages ----------------
    # ---------------- Helpers ----------------
    def _get_hr_stats():
        pending_hr = 0
        for coll in ["leave_requests", "visa_applications", "flight_requests", "cab_requests", "insurance_applications"]:
            pending_hr += db[coll].count_documents({"status": "SupervisorApproved"})
        pending_docs = db.documents.count_documents({"status": {"$in": ["Pending", "SupervisorApproved"]}})
        return {
            "total_employees": db.users.count_documents({}),
            "pending_hr": pending_hr,
            "pending_docs": pending_docs
        }

    def get_employee_stats(emp_id):
        # Count all pending requests
        pending = 0
        for coll in ["leave_requests", "visa_applications", "flight_requests", "cab_requests", "insurance_applications"]:
            pending += db[coll].count_documents({"employee_id": emp_id, "status": {"$in": ["Pending", "SupervisorApproved"]}})
        
        docs_count = db.documents.count_documents({"employee_id": emp_id})
        return {
            "pending_requests": pending,
            "docs_count": docs_count
        }

    @app.get("/employee/my-info")
    @login_required
    @ensure_role("employee","supervisor","hr","itadmin")
    def employee_my_info():
        user = current_user()
        stats = get_employee_stats(user["id"])
        return render_template("employee/my_info.html", user=user, stats=stats)

    # Documents (employee)
    @app.get("/employee/documents")
    @login_required
    @ensure_role("employee","supervisor","hr","itadmin")
    def employee_documents():
        user = current_user()
        stats = get_employee_stats(user["id"])
        docs = list(db.documents.find({"employee_id": user["id"]}).sort("upload_date", ASCENDING))
        return render_template("employee/documents.html", user=user, docs=docs, stats=stats)

    @app.post("/employee/documents/upload")
    @login_required
    @ensure_role("employee","supervisor","hr","itadmin")
    def employee_documents_upload():
        u = current_user()
        f = request.form
        name = f.get("name"); dtype = f.get("type")
        if not name or not dtype:
            flash("Document name and type are required.", "danger")
            return redirect(url_for("employee_documents"))
        initial_status = "Pending" if u.get("supervisor") else "SupervisorApproved"
        db.documents.insert_one({
            "employee_id": u["id"],
            "name": name, "type": dtype,
            "upload_date": datetime.utcnow().strftime('%Y-%m-%d'),
            "status": initial_status,
            "file_name": f.get("file_name") or "file.pdf",
            "file_server_path": f"/fileserver/employees/{u['id']}/documents/" + (f.get("file_name") or "file.pdf"),
            "created_at": datetime.utcnow()
        })
        flash("Document uploaded (metadata only).", "success")
        return redirect(url_for("employee_documents"))

    # Leave
    @app.get("/employee/leave")
    @login_required
    @ensure_role("employee","supervisor","hr","itadmin")
    def employee_leave_list():
        u = current_user()
        stats = get_employee_stats(u["id"])
        items = list(db.leave_requests.find({"employee_id": u["id"]}).sort("created_at", ASCENDING))
        return render_template("employee/leave_list.html", user=u, items=items, stats=stats)

    @app.post("/employee/leave/new")
    @login_required
    @ensure_role("employee","supervisor","hr","itadmin")
    def employee_leave_new():
        u = current_user(); f = request.form
        initial_status = "Pending" if u.get("supervisor") else "SupervisorApproved"
        db.leave_requests.insert_one({
            "employee_id": u["id"],
            "type": f.get("type"),
            "from_date": f.get("from_date"),
            "to_date": f.get("to_date"),
            "days": int(f.get("days") or 1),
            "reason": f.get("reason"),
            "status": initial_status,
            "created_at": datetime.utcnow()
        })
        flash("Leave request submitted.", "success")
        return redirect(url_for("employee_leave_list"))

    # Visa applications (employee)
    @app.get("/employee/visa")
    @login_required
    @ensure_role("employee","supervisor","hr","itadmin")
    def employee_visa_list():
        u = current_user()
        stats = get_employee_stats(u["id"])
        items = list(db.visa_applications.find({"employee_id": u["id"]}).sort("created_at", ASCENDING))
        return render_template("employee/visa_list.html", user=u, items=items, stats=stats)

    @app.post("/employee/visa/new")
    @login_required
    @ensure_role("employee","supervisor","hr","itadmin")
    def employee_visa_new():
        u = current_user(); f = request.form
        initial_status = "Pending" if u.get("supervisor") else "SupervisorApproved"
        db.visa_applications.insert_one({
            "employee_id": u["id"],
            "type": f.get("type"),
            "country": f.get("country"),
            "reason": f.get("reason"),
            "status": initial_status,
            "created_at": datetime.utcnow()
        })
        flash("Visa application submitted.", "success")
        return redirect(url_for("employee_visa_list"))

    # Flight
    @app.get("/employee/flight")
    @login_required
    @ensure_role("employee","supervisor","hr","itadmin")
    def employee_flight_list():
        u = current_user()
        stats = get_employee_stats(u["id"])
        items = list(db.flight_requests.find({"employee_id": u["id"]}).sort("created_at", ASCENDING))
        return render_template("employee/flight_list.html", user=u, items=items, stats=stats)

    @app.post("/employee/flight/new")
    @login_required
    @ensure_role("employee","supervisor","hr","itadmin")
    def employee_flight_new():
        u = current_user(); f = request.form
        initial_status = "Pending" if u.get("supervisor") else "SupervisorApproved"
        db.flight_requests.insert_one({
            "employee_id": u["id"],
            "type": f.get("type"),
            "origin": f.get("origin"),
            "destination": f.get("destination"),
            "date": f.get("date"),
            "reason": f.get("reason"),
            "status": initial_status,
            "created_at": datetime.utcnow()
        })
        flash("Flight request submitted.", "success")
        return redirect(url_for("employee_flight_list"))

    # Insurance (new)
    @app.get("/employee/insurance")
    @login_required
    @ensure_role("employee","supervisor","hr","itadmin")
    def employee_insurance_list():
        u = current_user()
        stats = get_employee_stats(u["id"])
        items = list(db.insurance_applications.find({"employee_id": u["id"]}).sort("created_at", ASCENDING))
        return render_template("employee/insurance_list.html", user=u, items=items, stats=stats)

    @app.post("/employee/insurance/new")
    @login_required
    @ensure_role("employee","supervisor","hr","itadmin")
    def employee_insurance_new():
        u = current_user(); f = request.form
        initial_status = "Pending" if u.get("supervisor") else "SupervisorApproved"
        db.insurance_applications.insert_one({
            "employee_id": u["id"],
            "coverage_type": f.get("coverage_type"),
            "dependents": int(f.get("dependents") or 0),
            "notes": f.get("notes"),
            "status": initial_status,
            "created_at": datetime.utcnow()
        })
        flash("Insurance application submitted.", "success")
        return redirect(url_for("employee_insurance_list"))

    # Cab
    @app.get("/employee/cab")
    @login_required
    @ensure_role("employee","supervisor","hr","itadmin")
    def employee_cab_list():
        u = current_user()
        stats = get_employee_stats(u["id"])
        items = list(db.cab_requests.find({"employee_id": u["id"]}).sort("created_at", ASCENDING))
        return render_template("employee/cab_list.html", user=u, items=items, stats=stats)

    @app.post("/employee/cab/new")
    @login_required
    @ensure_role("employee","supervisor","hr","itadmin")
    def employee_cab_new():
        u = current_user(); f = request.form
        initial_status = "Pending" if u.get("supervisor") else "SupervisorApproved"
        db.cab_requests.insert_one({
            "employee_id": u["id"],
            "date": f.get("date"),
            "time": f.get("time"),
            "origin": f.get("origin"),
            "destination": f.get("destination"),
            "reason": f.get("reason"),
            "status": initial_status,
            "created_at": datetime.utcnow(),
            "driver": None
        })
        flash("Cab request submitted.", "success")
        return redirect(url_for("employee_cab_list"))

    # ---------------- Supervisor ----------------
    @app.get("/supervisor/my-info")
    @login_required
    @ensure_role("supervisor")
    def supervisor_my_info():
        user = current_user()
        stats = get_employee_stats(user["id"])
        team = list(db.users.find({"supervisor": user["id"]}))
        return render_template("supervisor/my_info.html", user=user, team=team, stats=stats)

    @app.get("/supervisor/approvals")
    @login_required
    @ensure_role("supervisor")
    def supervisor_approvals():
        sup = current_user()
        team_ids = list({u["id"] for u in db.users.find({"supervisor": sup["id"]}, {"id":1})})
        
        pending_data = {
            "leave": list(db.leave_requests.find({"employee_id":{"$in":team_ids},"status":"Pending"})),
            "visa": list(db.visa_applications.find({"employee_id":{"$in":team_ids},"status":"Pending"})),
            "flight": list(db.flight_requests.find({"employee_id":{"$in":team_ids},"status":"Pending"})),
            "cab": list(db.cab_requests.find({"employee_id":{"$in":team_ids},"status":"Pending"})),
            "insurance": list(db.insurance_applications.find({"employee_id":{"$in":team_ids},"status":"Pending"}))
        }
        
        history_data = {
            "leave": list(db.leave_requests.find({"employee_id":{"$in":team_ids},"status":{"$in":["SupervisorApproved", "Approved", "Rejected"]}})),
            "visa": list(db.visa_applications.find({"employee_id":{"$in":team_ids},"status":{"$in":["SupervisorApproved", "Approved", "Rejected"]}})),
            "flight": list(db.flight_requests.find({"employee_id":{"$in":team_ids},"status":{"$in":["SupervisorApproved", "Approved", "Rejected"]}})),
            "cab": list(db.cab_requests.find({"employee_id":{"$in":team_ids},"status":{"$in":["SupervisorApproved", "Approved", "Rejected"]}})),
            "insurance": list(db.insurance_applications.find({"employee_id":{"$in":team_ids},"status":{"$in":["SupervisorApproved", "Approved", "Rejected"]}}))
        }
        return render_template("supervisor/approvals.html", pending_data=pending_data, history_data=history_data)

    @app.post("/supervisor/approve/<req_type>/<rid>")
    @login_required
    @ensure_role("supervisor")
    def supervisor_approve(req_type, rid):
        coll = {"leave":"leave_requests","visa":"visa_applications","flight":"flight_requests","cab":"cab_requests","insurance":"insurance_applications"}.get(req_type)
        if not coll: abort(404)
        app.db[coll].update_one({"_id": ObjectId(rid)}, {"$set":{"status":"SupervisorApproved"}})
        flash("Forwarded to HR.", "success")
        return redirect(url_for("supervisor_approvals"))

    @app.post("/supervisor/reject/<req_type>/<rid>")
    @login_required
    @ensure_role("supervisor")
    def supervisor_reject(req_type, rid):
        coll = {"leave":"leave_requests","visa":"visa_applications","flight":"flight_requests","cab":"cab_requests","insurance":"insurance_applications"}.get(req_type)
        if not coll: abort(404)
        rejection_reason = request.form.get("rejection_reason", "")
        update_data = {"status": "Rejected"}
        if rejection_reason:
            update_data["rejection_reason"] = rejection_reason
        app.db[coll].update_one({"_id": ObjectId(rid)}, {"$set": update_data})
        flash("Rejected.", "warning")
        return redirect(url_for("supervisor_approvals"))

    # ---------------- Employee Requests View ----------------
    @app.get("/employee-requests/<emp_id>")
    @login_required
    def view_employee_requests(emp_id):
        # Access control: Must be HR, IT Admin, or the employee's supervisor
        viewer_role = session.get("role")
        viewer_id = session.get("id")
        
        target_emp = app.db.users.find_one({"id": emp_id})
        if not target_emp:
            abort(404)
            
        if viewer_role not in ["hr", "itadmin"] and target_emp.get("supervisor") != viewer_id:
            abort(403)
            
        # Fetch all requests for this employee
        requests_data = {
            "leave": list(app.db.leave_requests.find({"employee_id": emp_id}).sort("created_at", DESCENDING)),
            "visa": list(app.db.visa_applications.find({"employee_id": emp_id}).sort("created_at", DESCENDING)),
            "flight": list(app.db.flight_requests.find({"employee_id": emp_id}).sort("created_at", DESCENDING)),
            "cab": list(app.db.cab_requests.find({"employee_id": emp_id}).sort("created_at", DESCENDING)),
            "insurance": list(app.db.insurance_applications.find({"employee_id": emp_id}).sort("created_at", DESCENDING)),
            "documents": list(app.db.documents.find({"employee_id": emp_id}).sort("upload_date", DESCENDING))
        }
        
        return render_template("hr/view_employee_requests.html", target_emp=target_emp, requests_data=requests_data)

    # ---------------- HR Directory ----------------
    @app.get("/hr/directory")
    @login_required
    @ensure_role("hr", "itadmin")
    def hr_directory():
        users = list(db.users.find().sort("id", ASCENDING))
        user = current_user()
        stats = get_employee_stats(user["id"])
        return render_template("hr/employee_directory.html", users=users, user=user, stats=stats)

    # ---------------- IT Admin ----------------
    @app.get("/it/employees")
    @login_required
    @ensure_role("hr", "itadmin")
    def it_employees():
        users = list(db.users.find().sort("id", ASCENDING))
        supervisors = list(db.users.find({"role": {"$in": ["supervisor", "hr", "itadmin"]}}, {"id": 1, "name": 1}).sort("name", 1))
        stats = _get_hr_stats()
        return render_template("itadmin/employees.html", users=users, supervisors=supervisors, stats=stats)

    @app.post("/it/employees/create")
    @login_required
    @ensure_role("hr", "itadmin")
    def it_create_employee():
        f = request.form
        emp_id = (f.get("id") or "").strip().lower()
        if not emp_id:
            flash("Employee ID is required.", "danger")
            return redirect(url_for("it_employees"))
        if app.db.users.find_one({"id": emp_id}):
            flash("Employee ID already exists.", "danger")
            return redirect(url_for("it_employees"))
        try:
            password = f.get("password") or os.urandom(8).hex()
            app.db.users.insert_one({
                "id": emp_id,
                "name": f.get("name"),
                "email": (f.get("email") or "").lower(),
                "role": f.get("role"),
                "department": f.get("department"),
                "designation": f.get("designation"),
                "supervisor": (f.get("supervisor") or "").strip().lower(),
                "leave_balance": int(f.get("leave_balance") or 14),
                "is_online": False,
                "password_hash": generate_password_hash(password)
            })
            flash(f"User {emp_id} created successfully.", "success")
        except Exception as e:
            flash(f"Error creating user: {str(e)}", "danger")
        return redirect(url_for("it_employees"))

    @app.post("/it/employees/delete/<oid>")
    @login_required
    @ensure_role("hr", "itadmin")
    def it_delete_employee(oid):
        if session.get("user_id") == oid:
            flash("You cannot delete yourself.", "danger")
            return redirect(url_for("it_employees"))
        app.db.users.delete_one({"_id": ObjectId(oid)})
        flash("User deleted.", "success")
        return redirect(url_for("it_employees"))

    @app.post("/it/employees/edit/<oid>")
    @login_required
    @ensure_role("hr", "itadmin")
    def it_edit_employee(oid):
        f = request.form
        try:
            update = {
                "id": (f.get("id") or "").strip().lower(),
                "name": f.get("name"),
                "email": (f.get("email") or "").lower(),
                "role": f.get("role"),
                "department": f.get("department"),
                "designation": f.get("designation"),
                "supervisor": (f.get("supervisor") or "").strip().lower(),
                "leave_balance": int(f.get("leave_balance") or 0)
            }
            app.db.users.update_one({"_id": ObjectId(oid)}, {"$set": update})
            flash("User updated.", "success")
        except Exception as e:
            flash(f"Error updating user: {str(e)}", "danger")
        return redirect(url_for("it_employees"))

    @app.post("/it/requests/delete/<req_type>/<rid>")
    @login_required
    @ensure_role("itadmin")
    def it_delete_request(req_type, rid):
        coll_map = {
            "leave": "leave_requests",
            "visa": "visa_applications",
            "flight": "flight_requests",
            "cab": "cab_requests",
            "insurance": "insurance_applications",
            "documents": "documents"
        }
        coll = coll_map.get(req_type)
        if not coll: abort(404)
        app.db[coll].delete_one({"_id": ObjectId(rid)})
        flash(f"{req_type.title()} request deleted.", "success")
        return redirect(request.referrer or url_for("dashboard"))

    # Document queues (HR)
    @app.get("/hr/documents/upload")
    @login_required
    @ensure_role("hr","itadmin")
    def hr_documents_upload_page():
        docs = list(db.documents.find().sort("upload_date", ASCENDING))
        users = list(db.users.find().sort("name", ASCENDING))
        stats = _get_hr_stats()
        return render_template("hr/upload_documents.html", docs=docs, users=users, stats=stats)

    @app.post("/hr/documents/upload_submit")
    @login_required
    @ensure_role("hr", "itadmin")
    def hr_documents_upload_submit():
        f = request.form
        employee_id = f.get("employee_id")
        dt_type = f.get("type")
        dt_name = f.get("name")
        dt_status = f.get("status", "Approved")
        
        if not employee_id or not dt_name or not dt_type:
            flash("Employee, Document Name, and Type are required.", "danger")
            return redirect(url_for("hr_documents_upload_page"))
            
        db.documents.insert_one({
            "employee_id": employee_id,
            "name": dt_name,
            "type": dt_type,
            "upload_date": datetime.utcnow().strftime('%Y-%m-%d'),
            "status": dt_status,
            "file_name": f.get("file_name") or "hr_upload.pdf",
            "file_server_path": f"/fileserver/employees/{employee_id}/documents/" + (f.get("file_name") or "hr_upload.pdf"),
            "created_at": datetime.utcnow()
        })
        flash("Document uploaded successfully.", "success")
        return redirect(url_for("hr_documents_upload_page"))

    @app.get("/hr/documents")
    @login_required
    @ensure_role("hr","itadmin")
    def hr_documents():
        # HR sees BOTH "Pending" (uploaded by employees) and "SupervisorApproved" (if any)
        docs = list(db.documents.find({"status": {"$in": ["Pending", "SupervisorApproved"]}}).sort("upload_date", ASCENDING))
        stats = _get_hr_stats()
        return render_template("hr/documents.html", docs=docs, stats=stats)

    @app.post("/hr/documents/<action>/<doc_id>")
    @login_required
    @ensure_role("hr","itadmin")
    def hr_documents_action(action, doc_id):
        update = {"reviewed_by": session.get("id"), "review_date": datetime.utcnow().strftime('%Y-%m-%d')}
        if action == "approve": 
            update["status"] = "Approved"
            update["rejection_reason"] = ""
        elif action == "reject": 
            update["status"] = "Rejected"
            update["rejection_reason"] = request.form.get("rejection_reason", "")
        else: 
            return redirect(url_for("hr_documents"))
        
        app.db.documents.update_one({"_id": ObjectId(doc_id)}, {"$set": update})
        flash("Document updated.", "success")
        return redirect(url_for("hr_documents"))

    # Approvals queues (HR)
    def _hr_queue(coll):
        return list(app.db[coll].find({"status":"SupervisorApproved"}).sort("created_at", ASCENDING))

    def _it_queue(coll):
        """IT Admin sees ALL requests regardless of status."""
        return list(app.db[coll].find({}).sort("created_at", DESCENDING))

    def _hr_history_queue(coll):
        return list(app.db[coll].find({"status": {"$in": ["Approved", "Rejected"]}}).sort("created_at", DESCENDING))

    # HR Leave Approvals
    @app.get("/hr/leave-approvals")
    @login_required
    @ensure_role("hr","itadmin")
    def hr_leave_approvals():
        # IT Admin sees everything; HR sees only supervisor-approved
        if session.get("role") == "itadmin":
            leaves = _it_queue("leave_requests")
            history = []
        else:
            leaves = list(db.leave_requests.find({"status":"SupervisorApproved"}).sort("start_date", ASCENDING))
            history = _hr_history_queue("leave_requests")
        stats = _get_hr_stats()
        return render_template("hr/leave_approvals.html", leaves=leaves, history=history, stats=stats)

    @app.get("/hr/visa-approvals")
    @login_required
    @ensure_role("hr","itadmin")
    def hr_visa_approvals():
        items = _it_queue("visa_applications") if session.get("role") == "itadmin" else _hr_queue("visa_applications")
        history = [] if session.get("role") == "itadmin" else _hr_history_queue("visa_applications")
        stats = _get_hr_stats()
        return render_template("hr/visa_approvals.html", items=items, history=history, req_type="visa", stats=stats)

    @app.get("/hr/flight-approvals")
    @login_required
    @ensure_role("hr","itadmin")
    def hr_flight_approvals():
        items = _it_queue("flight_requests") if session.get("role") == "itadmin" else _hr_queue("flight_requests")
        history = [] if session.get("role") == "itadmin" else _hr_history_queue("flight_requests")
        stats = _get_hr_stats()
        return render_template("hr/flight_approvals.html", items=items, history=history, req_type="flight", stats=stats)

    @app.get("/hr/insurance-approvals")
    @login_required
    @ensure_role("hr","itadmin")
    def hr_insurance_approvals():
        items = _it_queue("insurance_applications") if session.get("role") == "itadmin" else _hr_queue("insurance_applications")
        history = [] if session.get("role") == "itadmin" else _hr_history_queue("insurance_applications")
        stats = _get_hr_stats()
        return render_template("hr/insurance_approvals.html", items=items, history=history, req_type="insurance", stats=stats)

    @app.get("/hr/cab-approvals")
    @login_required
    @ensure_role("hr","itadmin")
    def hr_cab_approvals():
        items = _it_queue("cab_requests") if session.get("role") == "itadmin" else _hr_queue("cab_requests")
        history = [] if session.get("role") == "itadmin" else _hr_history_queue("cab_requests")
        stats = _get_hr_stats()
        return render_template("hr/cab_approvals.html", items=items, history=history, req_type="cab", stats=stats)

    @app.post("/hr/approve/<req_type>/<rid>")
    @login_required
    @ensure_role("hr","itadmin")
    def hr_approve(req_type, rid):
        coll = {"leave":"leave_requests","visa":"visa_applications","flight":"flight_requests","insurance":"insurance_applications","cab":"cab_requests"}.get(req_type)
        if not coll: abort(404)

        if req_type == "leave":
            req = app.db[coll].find_one({"_id": ObjectId(rid)})
            if req and req.get("status") != "Approved":
                emp_id = req.get("employee_id")
                days = int(req.get("days") or 0)
                if emp_id and days > 0:
                    app.db.users.update_one({"id": emp_id}, {"$inc": {"leave_balance": -days}})

        update_data = {"status": "Approved"}
        if req_type == "cab":
            driver_name = request.form.get("driver_name")
            driver_phone = request.form.get("driver_phone")
            if driver_name: update_data["driver_name"] = driver_name
            if driver_phone: update_data["driver_phone"] = driver_phone

        app.db[coll].update_one({"_id": ObjectId(rid)}, {"$set": update_data})
        flash("Approved.", "success")
        return redirect(request.referrer or url_for("hr_employees"))

    @app.post("/hr/reject/<req_type>/<rid>")
    @login_required
    @ensure_role("hr","itadmin")
    def hr_reject(req_type, rid):
        coll = {"leave":"leave_requests","visa":"visa_applications","flight":"flight_requests","insurance":"insurance_applications","cab":"cab_requests"}.get(req_type)
        if not coll: abort(404)
        rejection_reason = request.form.get("rejection_reason", "")
        update_data = {"status": "Rejected"}
        if rejection_reason:
            update_data["rejection_reason"] = rejection_reason
        app.db[coll].update_one({"_id": ObjectId(rid)}, {"$set": update_data})
        flash("Rejected.", "warning")
        return redirect(request.referrer or url_for("hr_employees"))

    # Visa management (master list)
    @app.get("/hr/visas")
    @login_required
    @ensure_role("hr","itadmin")
    def hr_visas():
        items = list(db.visas.find().sort("employee_id", ASCENDING))
        users = {u["id"]: u for u in db.users.find({}, {"id":1,"name":1,"_id":0})}
        stats = _get_hr_stats()
        return render_template("hr/visas.html", items=items, users=users, stats=stats)

    @app.post("/hr/visas/add")
    @login_required
    @ensure_role("hr", "itadmin")
    def hr_visas_add():
        f = request.form
        emp_id = f.get("employee_id")
        issue = f.get("issue_date"); expiry = f.get("expiry_date")
        db.visas.insert_one({
            "employee_id": emp_id,
            "type": f.get("type"),
            "country": f.get("country"),
            "visa_number": f.get("visa_number"),
            "issue_date": issue,
            "expiry_date": expiry,
            "status": "Active",
            "created_at": datetime.utcnow()
        })
        flash("Visa added.", "success")
        return redirect(url_for("hr_visas"))

    @app.post("/hr/visas/update/<vid>")
    @login_required
    @ensure_role("hr", "itadmin")
    def hr_visas_update(vid):
        f = request.form
        app.db.visas.update_one({"_id": ObjectId(vid)}, {"$set":{
            "type": f.get("type"),
            "country": f.get("country"),
            "visa_number": f.get("visa_number"),
            "issue_date": f.get("issue_date"),
            "expiry_date": f.get("expiry_date"),
            "status": f.get("status") or "Active"
        }})
        flash("Visa updated.", "success")
        return redirect(url_for("hr_visas"))

    @app.post("/hr/visas/delete/<vid>")
    @login_required
    @ensure_role("hr", "itadmin")
    def hr_visas_delete(vid):
        app.db.visas.delete_one({"_id": ObjectId(vid)})
        flash("Visa deleted.", "warning")
        return redirect(url_for("hr_visas"))

    # Reports
    def _get_report_data(form_data):
        all_reqs = []
        for coll, rtype, date_field in [
            ("leave_requests","Leave","created_at"),
            ("visa_applications","Visa","created_at"),
            ("flight_requests","Flight","created_at"),
            ("insurance_applications","Insurance","created_at"),
            ("cab_requests","Cab","created_at"),
        ]:
            for r in app.db[coll].find():
                all_reqs.append({
                    "employee_id": r.get("employee_id"),
                    "type": rtype,
                    "date": r.get("request_date") or r.get("application_date") or (r.get(date_field).strftime('%Y-%m-%d') if r.get(date_field) else ""),
                    "status": r.get("status")
                })
                
        dept = form_data.get("department")
        supervisor = form_data.get("supervisor")
        from_date = form_data.get("from_date")
        to_date = form_data.get("to_date")
        users = {u["id"]: u for u in app.db.users.find()}
        
        def ok(rec):
            u = users.get(rec["employee_id"]) or {}
            if dept and u.get("department") != dept: return False
            if supervisor and u.get("supervisor") != supervisor: return False
            if from_date and rec["date"] and rec["date"] < from_date: return False
            if to_date and rec["date"] and rec["date"] > to_date: return False
            return True
            
        rows = [r for r in all_reqs if ok(r)]
        for r in rows:
            u = users.get(r["employee_id"]) or {}
            r["emp_name"] = u.get("name") or r["employee_id"]
            r["department"] = u.get("department", "-")
            r["sup_id"] = u.get("supervisor")
            r["sup_name"] = users.get(r["sup_id"], {}).get("name", "-") if r["sup_id"] else "-"
            
        return rows, form_data

    @app.route("/hr/reports", methods=["GET", "POST"])
    @login_required
    @ensure_role("hr","itadmin")
    def hr_reports():
        users = list(db.users.find())
        sups = [u for u in users if u.get("role") in ("supervisor","hr")]
        pending_hr = 0
        for coll in ["leave_requests", "visa_applications", "flight_requests", "cab_requests", "insurance_applications"]:
            pending_hr += db[coll].count_documents({"status":"SupervisorApproved"})
        pending_docs = db.documents.count_documents({"status":"SupervisorApproved"})
        stats = {
            "total_employees": db.users.count_documents({}),
            "pending_hr": pending_hr,
            "pending_docs": pending_docs
        }
        
        report_data = None
        filters = {}
        if request.method == "POST":
            report_data, filters = _get_report_data(request.form)
            
        return render_template("hr/reports.html", supervisors=sups, stats=stats, report_data=report_data, filters=filters)

    @app.post("/hr/reports/export")
    @login_required
    @ensure_role("hr","itadmin")
    def hr_reports_export():
        rows, _ = _get_report_data(request.form)
        # Build CSV using enriched fields already populated by _get_report_data
        lines = ["Employee ID,Employee Name,Department,Supervisor,Request Type,Request Date,Status"]
        for r in rows:
            emp_id = r.get("employee_id", "-")
            emp_name = r.get("emp_name", emp_id).replace(",", " ")
            dept = r.get("department", "-").replace(",", " ")
            sup = r.get("sup_name", "-").replace(",", " ")
            req_type = r.get("type", "-")
            date = r.get("date", "-")
            status = r.get("status", "-")
            lines.append(f"{emp_id},{emp_name},{dept},{sup},{req_type},{date},{status}")
        csv_content = "\n".join(lines)
        resp = make_response(csv_content)
        resp.headers['Content-Type'] = 'text/csv'
        resp.headers['Content-Disposition'] = 'attachment; filename=HRMS_Report.csv'
        return resp


    # Errors
    @app.errorhandler(403)
    def e403(e): return render_template("base.html", content="Forbidden"), 403
    @app.errorhandler(404)
    def e404(e): return render_template("base.html", content="Not Found"), 404

    return app

if __name__ == "__main__":
    app = create_app()
    debug = os.environ.get("FLASK_DEBUG", "True").lower() == "true"
    port = int(os.environ.get("FLASK_PORT", 5002))
    app.run(debug=debug, port=port)

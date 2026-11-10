# MediSlot dashboard (Flask frontend)

Run locally:  pip install -r requirements.txt, copy .env.example to .env (fill values), then python app.py
Production:   gunicorn -w 1 --threads 4 -b 0.0.0.0:8000 app:app   (one worker keeps token refresh race-free)

Routes: / landing, /login, /register, /doctor/* (overview, schedule, appointments, availability,
blackouts, consultations, profile, settings), /patient/* (overview, doctors, appointments, profile)

Files: app.py (routes, role guards, CSRF, /api proxy), api.py (API Gateway client + token refresh),
static/js/core.js + doctor.js + patient.js, static/css/medislot.css, templates/

The browser never calls API Gateway: Flask keeps the JWT in its server-side session and proxies
/api/doctor/* and the patient routes, so no CORS setup is needed.
Patient pages (Milestone 4) use `patient/profile`, `doctors`, `doctors/filters`, `doctors/{id}` and
`doctors/{id}/slots`. Appointments are not built until Milestone 5, so that page still shows a
"not switched on yet" state, and the Book button is disabled. See `docs/milestone-4.md`.

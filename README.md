# RumCHUO

1. PROJECT OVERVIEW
RumCHUO is a student accommodation and roommate-finding platform designed to help university students find rooms, connect with potential roommates, and access verified accommodation providers.

2. MAIN OBJECTIVES
- Simplify student accommodation searches.
- Connect students looking for roommates.
- Help property Owners list their rooms.
- Allow Agents (Dalali) to submit and manage properties.
- Improve accommodation verification and trust.
- Connect students with accommodation support services.

3. USER ROLES
- Students: Search for rooms, manage profiles and find roommates.
- Owners: Submit properties and manage their accommodation listings.
- Agents (Dalali): Submit properties and facilitate accommodation services.
- Administrators: Manage users, verify properties and oversee platform activities.

4. MAIN FEATURES
- Student registration and login.
- Authentication and profile management.
- Room listing and searching.
- Property submission and verification.
- Owner and Agent management.
- Roommate matching.
- Administrative dashboard.
- Student dashboard.
- Agent dashboard.

5. TECHNOLOGY STACK
 - Component	Technology
 - Backend	Python
 - Framework	FastAPI
 - Database	SQLite
 - API documentation	Swagger UI
 - Authentication	JWT
 - Password security	Argon2
 - Frontend	HTML, CSS, JavaScript
 - Version control	Git and GitHub

6. PROJECT STRUCTURE
The repository includes files such as:
rumchuo/
├── main.py
├── admin_routes.py
├── agent_property_routes.py
├── index.html
├── student_dashboard.html
├── agent_dashboard.html
├── rumchuo.db
├── .env
└── README.md


7. INSTALLATION AND RUNNING
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install fastapi "uvicorn[standard]"
python -m uvicorn main:app --reload



# RumCHUO – Student Accommodation & Roommate Matching Platform

**RumCHUO** is a web-based student accommodation and roommate-finding platform designed to simplify how university students find accommodation, connect with potential roommates, and access accommodation services.

The platform connects students, property owners, and accommodation agents through a centralized system with administrative oversight.

## 1. Project Overview

Finding suitable student accommodation can be challenging, especially when students have limited access to reliable property information and compatible roommates.

RumCHUO aims to make this process easier by providing a platform where students can discover accommodation, connect with potential roommates, and interact with accommodation providers.

## 2. Key Features

### Student Features

- Student registration and authentication.
- Personal profile management.
- Accommodation search and room listings.
- Roommate matching.
- Property enquiries.
- Student accommodation support services.

### Property Owner Features

- Property and room submission.
- Property management dashboard.
- Property photograph management.
- Room availability management.
- Property approval status tracking.

### Agent Features

- Agent registration and management.
- Property submission and management.
- Multiple property photograph uploads.
- Property approval tracking.
- Accommodation enquiry management.

### Administrator Features

- Centralized administration dashboard.
- User and account management.
- Property verification and approval.
- Owner and agent management.
- Room and accommodation management.
- Platform activity oversight.

## 3. Technology Stack

| Component         | Technology            |
| ----------------- | --------------------- |
| Backend Language  | Python                |
| Backend Framework | FastAPI               |
| Frontend          | HTML, CSS, JavaScript |
| Database          | SQLite                |
| API Documentation | Swagger UI / OpenAPI  |
| Authentication    | JWT                   |
| Password Security | Argon2                |
| Version Control   | Git and GitHub        |

## 4. System Architecture

RumCHUO uses a backend API architecture.

- **Frontend:** HTML, CSS and JavaScript provide the user interface and interactive dashboards.
- **Backend:** FastAPI handles API requests, authentication, permissions and application logic.
- **Database:** SQLite stores application data, including user accounts, properties and accommodation information.

The frontend communicates with the backend through API requests.

## 5. Project Structure

The project contains several frontend pages and backend modules.

rumchuo/
│
├── main.py
├── admin\*routes.py
├── agent_dashboard_routes.py
├── agent_property_routes.py
├── student_services_routes.py
├── admin_setup.py
│
├── index.html
├── login.html
├── agent_login.html
├── reset_password.html
│
├── admin_dashboard.html
├── student_dashboard.html
├── agent_dashboard.html
├── landlord_dashboard.html
│
├── requirements.txt
├── README.md
└── .gitignore

## 6. Installation and Setup

### Prerequisites

- Python 3.10 or later
- Git
- A code editor such as Visual Studio Code

### Step 1: Clone the repository

```bash
git clone https://github.com/workholic3700-dot/tech.git
```

### Step 2: Enter the project directory

```bash
cd tech
```

### Step 3: Create a virtual environment

```bash
python -m venv .venv
```

### Step 4: Activate the virtual environment

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

### Step 5: Install dependencies

If a `requirements.txt` file is available:

```bash
pip install -r requirements.txt
```

Otherwise, install the dependencies required by the project.

### Step 6: Start the development server

```bash
python -m uvicorn main:app --reload
```

### Step 7: Access the application

Open your browser:

- Application: http://127.0.0.1:8000
- API Documentation: http://127.0.0.1:8000/docs

## 7. User Roles

RumCHUO supports four main user roles:

| Role          | Responsibility                    |
| ------------- | --------------------------------- |
| Student       | Find accommodation and roommates  |
| Owner         | Submit and manage properties      |
| Agent         | Facilitate accommodation services |
| Administrator | Manage and oversee the platform   |

## 8. Project Goals

RumCHUO aims to:

- Make student accommodation searches easier.
- Improve access to accommodation information.
- Connect students with compatible roommates.
- Support property owners and accommodation agents.
- Improve property verification and accountability.
- Provide a centralized student accommodation ecosystem.

## 9. Future Improvements

Potential areas for further development include:

- Enhanced roommate matching algorithms.
- Online payments and booking integration.
- Improved property verification.
- Mobile application integration.
- Notifications and messaging.
- Advanced analytics and reporting.

## 10. Developer

**Developed by:** George Mahinyila

**GitHub:** https://github.com/workholic3700-dot

---

_RumCHUO – Making student accommodation easier, safer and more accessible._

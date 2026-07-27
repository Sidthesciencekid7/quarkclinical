# PK/BE Protocol Platform

A comprehensive protocol management platform for Pharmacokinetic/Bioequivalence studies with AI-powered data extraction, compliance tracking, and FDA-aligned audit trails.

## Stack

### Frontend
- **React 18** - UI framework
- **Vite** - Build tool
- **TailwindCSS** - Styling
- **TipTap** - Protocol editor

### Backend
- **FastAPI** - Python web framework (3.11+)
- **PostgreSQL 15** - Database
- **SQLAlchemy ORM** - Database abstraction
- **Alembic** - Database migrations
- **Anthropic Claude API (claude-sonnet-4-6)** - AI extraction
- **python-docx** - Document generation

## Project Structure

```
/
├── frontend/          → React application
├── backend/           → FastAPI application
│   └── alembic/       → Database migrations
└── README.md          → This file
```

## Key Rules & Constraints

### Data Storage
- All protocol data must be stored as **versioned JSONB** in PostgreSQL
- Never store raw HTML - use structured JSONB format

### AI Data Extraction
- Every AI-extracted PK value must include `source_pmid` field
- No unverified data should be persisted
- Source tracking is mandatory for compliance

### Audit & Compliance
- All writes to `audit_logs` table must be **append-only** (no update/delete endpoints)
- **21 CFR Part 11 compliance** required for all read/write/export operations
- Complete audit trail must be maintained for regulatory inspection
- FDA BE guideline version must be **locked to each protocol at creation time**

## Environment Setup

### Required Environment Variables

Create a `.env` file in the project root with the following:

```
ANTHROPIC_API_KEY=<your-anthropic-api-key>
DATABASE_URL=postgresql://user:password@localhost:5432/pkbe_platform
PUBMED_API_KEY=<your-pubmed-api-key>
```

### Installation

1. **Backend Setup**
   ```bash
   cd backend
   pip install -r requirements.txt
   ```

2. **Frontend Setup**
   ```bash
   cd frontend
   npm install
   ```

3. **Database Setup**
   ```bash
   cd backend
   alembic upgrade head
   ```

## Running the Application

### Backend
```bash
cd backend
uvicorn main:app --reload
```

### Frontend
```bash
cd frontend
npm run dev
```

## Compliance Notes

This platform is designed to meet:
- **21 CFR Part 11** - Electronic Records; Electronic Signatures
- **FDA Bioequivalence Guidelines** - Protocol version locking and audit trails
- **GCP Principles** - Good Clinical Practice standards for protocol management

All data modifications are logged with timestamps and user attribution for regulatory compliance.

"""Import every model so Alembic autogenerate and relationship resolution see the full schema."""

from app.db.models.ai import (
    AgentRun,
    AiUsageRecord,
    CopilotConversation,
    CopilotMessage,
    IntegrationConnection,
    RetrievalQueryLog,
    ToolExecutionLog,
)
from app.db.models.bids import (
    BidApproval,
    BidArtifact,
    BidComment,
    BidDraft,
    BidStatus,
    BidTask,
    BidWorkspace,
    BoqImport,
    BoqItem,
    BoqScenario,
    TaskStatus,
)
from app.db.models.company import (
    CompanyCertification,
    CompanyFinancialRecord,
    CompanyProduct,
    CompanyProfile,
    CompanyProject,
    CompanyRegistration,
    FactStatus,
)
from app.db.models.documents import (
    Document,
    DocumentCategory,
    DocumentChunk,
    DocumentField,
    DocumentPage,
    DocumentScope,
    DocumentVersion,
    JobStatus,
    ProcessingJob,
    ProcessingStatus,
)
from app.db.models.eligibility import (
    AssessmentStatus,
    DecisionStatus,
    EligibilityAssessment,
    EligibilityDecision,
    EvidenceConflict,
    RequirementEvidenceLink,
    ReviewEvent,
)
from app.db.models.identity import (
    ApiToken,
    AuthToken,
    Invitation,
    Organization,
    OrganizationMember,
    OrgStatus,
    Role,
    User,
    UserSession,
)
from app.db.models.ops import (
    AuditEvent,
    BillingEvent,
    Notification,
    NotificationPreference,
    Plan,
    Subscription,
    UsageCounter,
)
from app.db.models.tenders import (
    Mandatory,
    RequirementType,
    ReviewStatus,
    SavedSearch,
    Tender,
    TenderAmendment,
    TenderDocument,
    TenderMatch,
    TenderRequirement,
    TenderStatus,
    TenderStatusEvent,
    TenderVersion,
)

__all__ = [n for n in dir() if not n.startswith("_")]

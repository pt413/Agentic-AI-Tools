from ..projections.identity_resolution_service import IdentityResolutionService
from ..projections.event_modeling_service import EventModelingService
from ..projections.participant_extraction_service import ParticipantExtractionService
from ..projections.event_context_projection_service import EventContextProjectionService
from ..projections.booking_fact_service import BookingFactService
from ..ingestion.source_ingestion_service import SourceIngestionService
from .checkpoint_service import ProcessorCheckpointService
from ..projections.lead_fact_service import LeadFactService
from ..projections.ontology_tagging_service import OntologyTaggingService
from ..projections.current_state_projection_service import CurrentStateProjectionService


class ServiceContainer:
    """
    Lightweight dependency container for independent analytics sync processors.

    Provides shared generic services used by all per-table sync classes.
    """

    def __init__(self, db):
        self.db = db

        # identity graph
        self.identity = IdentityResolutionService(db)

        # event timeline
        self.events = EventModelingService(db)

        # participants
        self.participants = ParticipantExtractionService(db, self.identity)

        # contexts
        self.context = EventContextProjectionService(db)

        # domain/business fact tables
        self.booking_facts = BookingFactService(db)
        self.lead_facts = LeadFactService(db)

        # semantic layer
        self.ontology = OntologyTaggingService(db)
        self.current_state = CurrentStateProjectionService(db, ontology_service=self.ontology)

        # staging/source fetchers
        self.source = SourceIngestionService(db)

        # checkpoint tracking
        self.checkpoints = ProcessorCheckpointService(db)


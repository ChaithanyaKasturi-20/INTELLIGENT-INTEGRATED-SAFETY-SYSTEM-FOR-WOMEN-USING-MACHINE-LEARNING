from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from .models import (
    CommunityReport,
    CommunityReportFlag,
    EmergencyContact,
    Journey,
    JourneyActivity,
    JourneyCheckInAlert,
    JourneyCheckInDelivery,
    SafetyTimer,
    SOSEmailDelivery,
    SOSEvent,
)


@admin.register(CommunityReport)
class CommunityReportAdmin(admin.ModelAdmin):
    list_display = ('id', 'category', 'incident_at', 'status', 'created_at', 'evidence_link')
    list_filter = ('status', 'category', 'incident_at')
    search_fields = ('description', 'reporter')
    readonly_fields = ('reporter', 'approximate_latitude', 'approximate_longitude', 'incident_at', 'moderated_by', 'moderated_at', 'created_at', 'updated_at', 'evidence_link')
    fields = (
        'reporter',
        'category',
        'description',
        'approximate_latitude',
        'approximate_longitude',
        'incident_at',
        'evidence_link',
        'status',
        'moderation_note',
        'moderated_by',
        'moderated_at',
        'created_at',
        'updated_at',
    )
    actions = ['mark_under_review', 'approve_reports', 'reject_reports']

    def get_urls(self):
        custom_urls = [
            path(
                '<int:report_id>/evidence/',
                self.admin_site.admin_view(self.download_evidence),
                name='safetyapp_communityreport_evidence',
            ),
        ]
        return custom_urls + super(CommunityReportAdmin, self).get_urls()

    def evidence_link(self, obj):
        if not obj or not obj.pk or not obj.evidence:
            return 'No evidence uploaded'
        url = reverse('admin:safetyapp_communityreport_evidence', args=[obj.pk])
        return format_html('<a href="{}">Download private evidence</a>', url)
    evidence_link.short_description = 'Evidence'

    def formfield_for_dbfield(self, db_field, **kwargs):
        if db_field.name == 'description':
            kwargs['help_text'] = (
                'Before approving, remove names, exact private addresses, and other identifying details. '
                'Approval is moderation, not independent verification.'
            )
        return super(CommunityReportAdmin, self).formfield_for_dbfield(db_field, **kwargs)

    def download_evidence(self, request, report_id):
        report = self.get_object(request, report_id)
        if report is None or not report.evidence:
            raise Http404
        if not self.has_view_permission(request, report):
            raise PermissionDenied
        extension = report.evidence.name.rsplit('.', 1)[-1].lower()
        content_type = 'image/png' if extension == 'png' else 'image/jpeg'
        response = FileResponse(report.evidence.open('rb'), content_type=content_type)
        response['Content-Disposition'] = 'attachment; filename="community-report-evidence.{}"'.format(extension)
        response['X-Content-Type-Options'] = 'nosniff'
        response['Cache-Control'] = 'private, no-store'
        return response

    def _set_status(self, request, queryset, status, message):
        changed = queryset.update(
            status=status,
            moderated_by=request.user.get_username(),
            moderated_at=timezone.now(),
            updated_at=timezone.now(),
        )
        self.message_user(request, '{} report(s) marked {}.'.format(changed, message))

    def mark_under_review(self, request, queryset):
        self._set_status(request, queryset, CommunityReport.STATUS_UNDER_REVIEW, 'under review')
    mark_under_review.short_description = 'Mark selected reports under review'

    def approve_reports(self, request, queryset):
        changed = queryset.update(
            status=CommunityReport.STATUS_APPROVED,
            moderated_by=request.user.get_username(),
            moderated_at=timezone.now(),
            updated_at=timezone.now(),
        )
        self.message_user(request, '{} report(s) approved. Approval does not mean independently verified.'.format(changed))
    approve_reports.short_description = 'Approve for public display after privacy review'

    def reject_reports(self, request, queryset):
        self._set_status(request, queryset, CommunityReport.STATUS_REJECTED, 'rejected')
    reject_reports.short_description = 'Reject selected reports'

    def save_model(self, request, obj, form, change):
        if change:
            previous = CommunityReport.objects.get(pk=obj.pk)
            if previous.status != obj.status:
                obj.moderated_by = request.user.get_username()
                obj.moderated_at = timezone.now()
        super(CommunityReportAdmin, self).save_model(request, obj, form, change)


@admin.register(CommunityReportFlag)
class CommunityReportFlagAdmin(admin.ModelAdmin):
    list_display = ('id', 'report', 'reason', 'status', 'reviewed_by', 'reviewed_at', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('reason', 'report__description', 'reporter')
    readonly_fields = ('report', 'reporter', 'reason', 'created_at', 'reviewed_by')
    actions = ['mark_flags_reviewed', 'dismiss_flags']

    def _mark_flags(self, request, queryset, status):
        now = timezone.now()
        changed = queryset.update(
            status=status,
            reviewed_by=request.user.get_username(),
            reviewed_at=now,
        )
        self.message_user(request, '{} flag(s) updated.'.format(changed))

    def mark_flags_reviewed(self, request, queryset):
        self._mark_flags(request, queryset, CommunityReportFlag.STATUS_REVIEWED)
    mark_flags_reviewed.short_description = 'Mark selected flags reviewed'

    def dismiss_flags(self, request, queryset):
        self._mark_flags(request, queryset, CommunityReportFlag.STATUS_DISMISSED)
    dismiss_flags.short_description = 'Dismiss selected flags'


@admin.register(Journey)
class JourneyAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'origin_label', 'destination_label', 'status', 'expected_arrival', 'guardian_enabled')
    list_filter = ('status', 'guardian_enabled')
    search_fields = ('user', 'origin_label', 'destination_label')
    readonly_fields = ('user', 'origin_label', 'destination_label', 'distance_meters', 'estimated_duration_seconds', 'planned_departure', 'expected_arrival', 'started_at', 'ended_at', 'status', 'guardian_enabled', 'guardian_contacts', 'created_at', 'updated_at')


@admin.register(JourneyActivity)
class JourneyActivityAdmin(admin.ModelAdmin):
    list_display = ('journey', 'action', 'occurred_at')
    list_filter = ('action', 'occurred_at')
    readonly_fields = ('journey', 'action', 'occurred_at', 'details')


@admin.register(JourneyCheckInAlert)
class JourneyCheckInAlertAdmin(admin.ModelAdmin):
    list_display = ('journey', 'deadline_at', 'status', 'created_at', 'completed_at')
    list_filter = ('status',)
    readonly_fields = ('journey', 'deadline_at', 'status', 'created_at', 'completed_at')


@admin.register(JourneyCheckInDelivery)
class JourneyCheckInDeliveryAdmin(admin.ModelAdmin):
    list_display = ('alert', 'contact', 'recipient_email', 'status', 'attempted_at', 'completed_at')
    list_filter = ('status',)
    readonly_fields = ('alert', 'contact', 'recipient_email', 'status', 'attempted_at', 'completed_at', 'error_message')


admin.site.register(EmergencyContact)
admin.site.register(SafetyTimer)
admin.site.register(SOSEvent)
admin.site.register(SOSEmailDelivery)

from django.db.models import Q

from .models import CartRun, ImportJob, RecipeRefinement


def active_tasks(request):
    if not request.user.is_authenticated:
        return {"active_task_count": 0}

    import_count = ImportJob.objects.filter(
        requested_by=request.user,
        status__in=[ImportJob.Status.PENDING, ImportJob.Status.PROCESSING],
    ).count()
    refinement_count = RecipeRefinement.objects.filter(
        requested_by=request.user,
        status__in=[
            RecipeRefinement.Status.PENDING,
            RecipeRefinement.Status.PROCESSING,
        ],
    ).count()
    cart_count = (
        CartRun.objects.filter(requested_by=request.user)
        .filter(
            Q(
                status__in=[
                    CartRun.Status.PENDING,
                    CartRun.Status.PROCESSING,
                    CartRun.Status.CLEANUP_PENDING,
                    CartRun.Status.CLEANING,
                    CartRun.Status.MANUAL_CHECK,
                ]
            )
            | Q(cleanup_requested_at__isnull=False, cleaned_at__isnull=True)
        )
        .count()
    )
    return {"active_task_count": import_count + refinement_count + cart_count}

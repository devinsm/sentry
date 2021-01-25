import six

from django.utils.translation import ugettext as _
from django.utils.safestring import mark_safe
from django.core.urlresolvers import reverse
from django.http import HttpResponseRedirect
from django.contrib import messages
from django.http import Http404

from sentry import options
from sentry.api import client
from sentry.api.serializers import serialize
from sentry.models import ProjectOption
from sentry.utils import json
from sentry.web.helpers import render_to_string


def react_plugin_config(plugin, project, request):
    response = client.get(
        "/projects/{}/{}/plugins/{}/".format(project.organization.slug, project.slug, plugin.slug),
        request=request,
    )
    nonce = ""
    if hasattr(request, "csp_nonce"):
        nonce = ' nonce="{}"'.format(request.csp_nonce)

    return mark_safe(
        """
    <div id="ref-plugin-config"></div>
    <script%s>
    $(function(){
        ReactDOM.render(React.createFactory(SentryApp.PluginConfig)({
            project: %s,
            organization: %s,
            data: %s
        }), document.getElementById('ref-plugin-config'));
    });
    </script>
    """
        % (
            nonce,
            json.dumps_htmlsafe(serialize(project, request.user)),
            json.dumps_htmlsafe(serialize(project.organization, request.user)),
            json.dumps_htmlsafe(response.data),
        )
    )


def default_plugin_config(plugin, project, request):
    if plugin.can_enable_for_projects() and not plugin.can_configure_for_project(project):
        raise Http404()

    plugin_key = plugin.get_conf_key()
    form_class = plugin.get_conf_form(project)
    template = plugin.get_conf_template(project)

    if form_class is None:
        return HttpResponseRedirect(
            reverse("sentry-manage-project", args=[project.organization.slug, project.slug])
        )

    test_results = None

    form = form_class(
        request.POST if request.POST.get("plugin") == plugin.slug else None,
        initial=plugin.get_conf_options(project),
        prefix=plugin_key,
    )
    if form.is_valid():
        if "action_test" in request.POST and plugin.is_testable():
            test_results = plugin.test_configuration_and_get_test_results(project)
        else:
            for field, value in six.iteritems(form.cleaned_data):
                key = "%s:%s" % (plugin_key, field)
                if project:
                    ProjectOption.objects.set_value(project, key, value)
                else:
                    options.set(key, value)

            messages.add_message(
                request, messages.SUCCESS, _("Your settings were saved successfully.")
            )
            return HttpResponseRedirect(request.path)

    # TODO(mattrobenolt): Reliably determine if a plugin is configured
    # if hasattr(plugin, 'is_configured'):
    #     is_configured = plugin.is_configured(project)
    # else:
    #     is_configured = True
    is_configured = True

    return mark_safe(
        render_to_string(
            template=template,
            context={
                "form": form,
                "plugin": plugin,
                "plugin_description": plugin.get_description() or "",
                "plugin_test_results": test_results,
                "plugin_is_configured": is_configured,
            },
            request=request,
        )
    )


def default_issue_plugin_config(plugin, project, form_data):
    plugin_key = plugin.get_conf_key()
    for field, value in six.iteritems(form_data):
        key = "%s:%s" % (plugin_key, field)
        if project:
            ProjectOption.objects.set_value(project, key, value)
        else:
            options.set(key, value)


def default_plugin_options(plugin, project):
    form_class = plugin.get_conf_form(project)
    if form_class is None:
        return {}

    NOTSET = object()
    plugin_key = plugin.get_conf_key()
    initials = plugin.get_form_initial(project)
    for field in form_class.base_fields:
        key = "%s:%s" % (plugin_key, field)
        if project is not None:
            value = ProjectOption.objects.get_value(project, key, NOTSET)
        else:
            value = options.get(key)
        if value is not NOTSET:
            initials[field] = value
    return initials

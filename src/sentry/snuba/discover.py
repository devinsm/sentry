import math
import sentry_sdk
import six
import logging

from collections import namedtuple

from sentry import options
from sentry.api.event_search import (
    FIELD_ALIASES,
    get_filter,
    get_function_alias,
    get_json_meta_type,
    is_function,
    InvalidSearchQuery,
    resolve_field_list,
)

from sentry.models import Group
from sentry.tagstore.base import TOP_VALUES_DEFAULT_LIMIT
from sentry.utils.compat import filter
from sentry.utils.math import nice_int
from sentry.utils.snuba import (
    Dataset,
    get_measurement_name,
    naiveify_datetime,
    raw_query,
    resolve_snuba_aliases,
    resolve_column,
    SNUBA_AND,
    SNUBA_OR,
    SnubaTSResult,
    to_naive_timestamp,
)

__all__ = (
    "PaginationResult",
    "InvalidSearchQuery",
    "query",
    "timeseries_query",
    "top_events_timeseries",
    "get_facets",
    "transform_data",
    "zerofill",
    "histogram_query",
)


logger = logging.getLogger(__name__)

PaginationResult = namedtuple("PaginationResult", ["next", "previous", "oldest", "latest"])
FacetResult = namedtuple("FacetResult", ["key", "value", "count"])

resolve_discover_column = resolve_column(Dataset.Discover)


def is_real_column(col):
    """
    Return true if col corresponds to an actual column to be fetched
    (not an aggregate function or field alias)
    """
    if is_function(col):
        return False

    if col in FIELD_ALIASES:
        return False

    return True


def resolve_discover_aliases(snuba_filter, function_translations=None):
    """
    Resolve the public schema aliases to the discover dataset.

    Returns a copy of the input structure, and includes a
    `translated_columns` key containing the selected fields that need to
    be renamed in the result set.
    """
    return resolve_snuba_aliases(
        snuba_filter, resolve_discover_column, function_translations=function_translations
    )


def zerofill(data, start, end, rollup, orderby):
    rv = []
    start = int(to_naive_timestamp(naiveify_datetime(start)) / rollup) * rollup
    end = (int(to_naive_timestamp(naiveify_datetime(end)) / rollup) * rollup) + rollup
    data_by_time = {}

    for obj in data:
        if obj["time"] in data_by_time:
            data_by_time[obj["time"]].append(obj)
        else:
            data_by_time[obj["time"]] = [obj]

    for key in six.moves.xrange(start, end, rollup):
        if key in data_by_time and len(data_by_time[key]) > 0:
            rv = rv + data_by_time[key]
            data_by_time[key] = []
        else:
            rv.append({"time": key})

    if "-time" in orderby:
        return list(reversed(rv))

    return rv


def transform_results(
    results, function_alias_map, translated_columns, snuba_filter, selected_columns=None
):
    results = transform_data(results, translated_columns, snuba_filter, selected_columns)
    results["meta"] = transform_meta(results, function_alias_map)
    return results


def transform_meta(results, function_alias_map):
    meta = {
        value["name"]: get_json_meta_type(
            value["name"], value.get("type"), function_alias_map.get(value["name"])
        )
        for value in results["meta"]
    }
    # Ensure all columns in the result have types.
    if results["data"]:
        for key in results["data"][0]:
            if key not in meta:
                meta[key] = "string"
    return meta


def transform_data(result, translated_columns, snuba_filter, selected_columns=None):
    """
    Transform internal names back to the public schema ones.

    When getting timeseries results via rollup, this function will
    zerofill the output results.
    """
    if selected_columns is None:
        selected_columns = []

    for col in result["meta"]:
        # Translate back column names that were converted to snuba format
        col["name"] = translated_columns.get(col["name"], col["name"])

    def get_row(row):
        transformed = {}
        for key, value in row.items():
            if isinstance(value, float) and math.isnan(value):
                value = 0
            transformed[translated_columns.get(key, key)] = value

        return transformed

    if len(translated_columns):
        result["data"] = [get_row(row) for row in result["data"]]

    rollup = snuba_filter.rollup
    if rollup and rollup > 0:
        with sentry_sdk.start_span(
            op="discover.discover", description="transform_results.zerofill"
        ) as span:
            span.set_data("result_count", len(result.get("data", [])))
            result["data"] = zerofill(
                result["data"], snuba_filter.start, snuba_filter.end, rollup, snuba_filter.orderby
            )

    return result


def query(
    selected_columns,
    query,
    params,
    orderby=None,
    offset=None,
    limit=50,
    referrer=None,
    auto_fields=False,
    auto_aggregations=False,
    use_aggregate_conditions=False,
    conditions=None,
    functions_acl=None,
):
    """
    High-level API for doing arbitrary user queries against events.

    This function operates on the Discover public event schema and
    virtual fields/aggregate functions for selected columns and
    conditions are supported through this function.

    The resulting list will have all internal field names mapped
    back into their public schema names.

    selected_columns (Sequence[str]) List of public aliases to fetch.
    query (str) Filter query string to create conditions from.
    params (Dict[str, str]) Filtering parameters with start, end, project_id, environment
    orderby (None|str|Sequence[str]) The field to order results by.
    offset (None|int) The record offset to read.
    limit (int) The number of records to fetch.
    referrer (str|None) A referrer string to help locate the origin of this query.
    auto_fields (bool) Set to true to have project + eventid fields automatically added.
    auto_aggregations (bool) Whether aggregates should be added automatically if they're used
                    in conditions, and there's at least one aggregate already.
    use_aggregate_conditions (bool) Set to true if aggregates conditions should be used at all.
    conditions (Sequence[any]) List of conditions that are passed directly to snuba without
                    any additional processing.
    """
    if not selected_columns:
        raise InvalidSearchQuery("No columns selected")

    # We clobber this value throughout this code, so copy the value
    selected_columns = selected_columns[:]

    with sentry_sdk.start_span(
        op="discover.discover", description="query.filter_transform"
    ) as span:
        span.set_data("query", query)

        snuba_filter = get_filter(query, params)
        if not use_aggregate_conditions:
            assert (
                not auto_aggregations
            ), "Auto aggregations cannot be used without enabling aggregate conditions"
            snuba_filter.having = []

    function_translations = {}

    with sentry_sdk.start_span(op="discover.discover", description="query.field_translations"):
        if orderby is not None:
            orderby = list(orderby) if isinstance(orderby, (list, tuple)) else [orderby]
            snuba_filter.orderby = [get_function_alias(o) for o in orderby]

        resolved_fields = resolve_field_list(
            selected_columns,
            snuba_filter,
            auto_fields=auto_fields,
            auto_aggregations=auto_aggregations,
            functions_acl=functions_acl,
        )

        snuba_filter.update_with(resolved_fields)

        # Resolve the public aliases into the discover dataset names.
        snuba_filter, translated_columns = resolve_discover_aliases(
            snuba_filter, function_translations
        )

        # Make sure that any aggregate conditions are also in the selected columns
        for having_clause in snuba_filter.having:
            # The first element of the having can be an alias, or a nested array of functions. Loop through to make sure
            # any referenced functions are in the aggregations.
            error_extra = ", and could not be automatically added" if auto_aggregations else ""
            if isinstance(having_clause[0], (list, tuple)):
                # Functions are of the form [fn, [args]]
                args_to_check = [[having_clause[0]]]
                conditions_not_in_aggregations = []
                while len(args_to_check) > 0:
                    args = args_to_check.pop()
                    for arg in args:
                        if arg[0] in [SNUBA_AND, SNUBA_OR]:
                            args_to_check.extend(arg[1])
                        # Only need to iterate on arg[1] if its a list
                        elif isinstance(arg[1], (list, tuple)):
                            alias = arg[1][0]
                            found = any(
                                alias == agg_clause[-1] for agg_clause in snuba_filter.aggregations
                            )
                            if not found:
                                conditions_not_in_aggregations.append(alias)

                if len(conditions_not_in_aggregations) > 0:
                    raise InvalidSearchQuery(
                        "Aggregate(s) {} used in a condition but are not in the selected columns{}.".format(
                            ", ".join(conditions_not_in_aggregations),
                            error_extra,
                        )
                    )
            else:
                found = any(
                    having_clause[0] == agg_clause[-1] for agg_clause in snuba_filter.aggregations
                )
                if not found:
                    raise InvalidSearchQuery(
                        "Aggregate {} used in a condition but is not a selected column{}.".format(
                            having_clause[0],
                            error_extra,
                        )
                    )

        if conditions is not None:
            snuba_filter.conditions.extend(conditions)

    with sentry_sdk.start_span(op="discover.discover", description="query.snuba_query"):
        result = raw_query(
            start=snuba_filter.start,
            end=snuba_filter.end,
            groupby=snuba_filter.groupby,
            conditions=snuba_filter.conditions,
            aggregations=snuba_filter.aggregations,
            selected_columns=snuba_filter.selected_columns,
            filter_keys=snuba_filter.filter_keys,
            having=snuba_filter.having,
            orderby=snuba_filter.orderby,
            dataset=Dataset.Discover,
            limit=limit,
            offset=offset,
            referrer=referrer,
        )

    with sentry_sdk.start_span(
        op="discover.discover", description="query.transform_results"
    ) as span:
        span.set_data("result_count", len(result.get("data", [])))
        return transform_results(
            result, resolved_fields["functions"], translated_columns, snuba_filter, selected_columns
        )


def get_timeseries_snuba_filter(selected_columns, query, params, rollup, default_count=True):
    snuba_filter = get_filter(query, params)
    if not snuba_filter.start and not snuba_filter.end:
        raise InvalidSearchQuery("Cannot get timeseries result without a start and end.")

    snuba_filter.update_with(resolve_field_list(selected_columns, snuba_filter, auto_fields=False))

    # Resolve the public aliases into the discover dataset names.
    snuba_filter, translated_columns = resolve_discover_aliases(snuba_filter)
    if not snuba_filter.aggregations:
        raise InvalidSearchQuery("Cannot get timeseries result with no aggregation.")

    # Change the alias of the first aggregation to count. This ensures compatibility
    # with other parts of the timeseries endpoint expectations
    if len(snuba_filter.aggregations) == 1 and default_count:
        snuba_filter.aggregations[0][2] = "count"

    return snuba_filter, translated_columns


def timeseries_query(selected_columns, query, params, rollup, referrer=None):
    """
    High-level API for doing arbitrary user timeseries queries against events.

    This function operates on the public event schema and
    virtual fields/aggregate functions for selected columns and
    conditions are supported through this function.

    This function is intended to only get timeseries based
    results and thus requires the `rollup` parameter.

    Returns a SnubaTSResult object that has been zerofilled in
    case of gaps.

    selected_columns (Sequence[str]) List of public aliases to fetch.
    query (str) Filter query string to create conditions from.
    params (Dict[str, str]) Filtering parameters with start, end, project_id, environment,
    rollup (int) The bucket width in seconds
    referrer (str|None) A referrer string to help locate the origin of this query.
    """
    with sentry_sdk.start_span(
        op="discover.discover", description="timeseries.filter_transform"
    ) as span:
        span.set_data("query", query)
        snuba_filter, _ = get_timeseries_snuba_filter(selected_columns, query, params, rollup)

    with sentry_sdk.start_span(op="discover.discover", description="timeseries.snuba_query"):
        result = raw_query(
            aggregations=snuba_filter.aggregations,
            conditions=snuba_filter.conditions,
            filter_keys=snuba_filter.filter_keys,
            start=snuba_filter.start,
            end=snuba_filter.end,
            rollup=rollup,
            orderby="time",
            groupby=["time"],
            dataset=Dataset.Discover,
            limit=10000,
            referrer=referrer,
        )

    with sentry_sdk.start_span(
        op="discover.discover", description="timeseries.transform_results"
    ) as span:
        span.set_data("result_count", len(result.get("data", [])))
        result = zerofill(result["data"], snuba_filter.start, snuba_filter.end, rollup, "time")

        return SnubaTSResult({"data": result}, snuba_filter.start, snuba_filter.end, rollup)


def create_result_key(result_row, fields, issues):
    values = []
    for field in fields:
        if field == "issue.id":
            values.append(issues.get(result_row["issue.id"], "unknown"))
        else:
            value = result_row.get(field)
            if isinstance(value, list):
                if len(value) > 0:
                    value = value[-1]
                else:
                    value = ""
            values.append(six.text_type(value))
    return ",".join(values)


def top_events_timeseries(
    timeseries_columns,
    selected_columns,
    user_query,
    params,
    orderby,
    rollup,
    limit,
    organization,
    referrer=None,
    top_events=None,
    allow_empty=True,
):
    """
    High-level API for doing arbitrary user timeseries queries for a limited number of top events

    Returns a dictionary of SnubaTSResult objects that have been zerofilled in
    case of gaps. Each value of the dictionary should match the result of a timeseries query

    timeseries_columns (Sequence[str]) List of public aliases to fetch for the timeseries query,
                    usually matches the y-axis of the graph
    selected_columns (Sequence[str]) List of public aliases to fetch for the events query,
                    this is to determine what the top events are
    user_query (str) Filter query string to create conditions from. needs to be user_query
                    to not conflict with the function query
    params (Dict[str, str]) Filtering parameters with start, end, project_id, environment,
    orderby (Sequence[str]) The fields to order results by.
    rollup (int) The bucket width in seconds
    limit (int) The number of events to get timeseries for
    organization (Organization) Used to map group ids to short ids
    referrer (str|None) A referrer string to help locate the origin of this query.
    top_events (dict|None) A dictionary with a 'data' key containing a list of dictionaries that
                    represent the top events matching the query. Useful when you have found
                    the top events earlier and want to save a query.
    """
    if top_events is None:
        with sentry_sdk.start_span(op="discover.discover", description="top_events.fetch_events"):
            top_events = query(
                selected_columns,
                query=user_query,
                params=params,
                orderby=orderby,
                limit=limit,
                referrer=referrer,
                auto_aggregations=True,
                use_aggregate_conditions=True,
            )

    with sentry_sdk.start_span(
        op="discover.discover", description="top_events.filter_transform"
    ) as span:
        span.set_data("query", user_query)
        snuba_filter, translated_columns = get_timeseries_snuba_filter(
            list(set(timeseries_columns + selected_columns)),
            user_query,
            params,
            rollup,
            default_count=False,
        )

        for field in selected_columns:
            # project is handled by filter_keys already
            if field in ["project", "project.id"]:
                continue
            if field in FIELD_ALIASES:
                field = FIELD_ALIASES[field].alias
            # Note that because orderby shouldn't be an array field its not included in the values
            values = list(
                {
                    event.get(field)
                    for event in top_events["data"]
                    if field in event and not isinstance(event.get(field), list)
                }
            )
            if values:
                # timestamp needs special handling, creating a big OR instead
                if field == "timestamp":
                    snuba_filter.conditions.append([["timestamp", "=", value] for value in values])
                elif None in values:
                    non_none_values = [value for value in values if value is not None]
                    condition = [[["isNull", [resolve_discover_column(field)]], "=", 1]]
                    if non_none_values:
                        condition.append([resolve_discover_column(field), "IN", non_none_values])
                    snuba_filter.conditions.append(condition)
                elif field in FIELD_ALIASES:
                    snuba_filter.conditions.append([field, "IN", values])
                else:
                    snuba_filter.conditions.append([resolve_discover_column(field), "IN", values])

    with sentry_sdk.start_span(op="discover.discover", description="top_events.snuba_query"):
        result = raw_query(
            aggregations=snuba_filter.aggregations,
            conditions=snuba_filter.conditions,
            filter_keys=snuba_filter.filter_keys,
            selected_columns=snuba_filter.selected_columns,
            start=snuba_filter.start,
            end=snuba_filter.end,
            rollup=rollup,
            orderby="time",
            groupby=["time"] + snuba_filter.groupby,
            dataset=Dataset.Discover,
            limit=10000,
            referrer=referrer,
        )

    if not allow_empty and not len(result.get("data", [])):
        return SnubaTSResult(
            {"data": zerofill([], snuba_filter.start, snuba_filter.end, rollup, "time")},
            snuba_filter.start,
            snuba_filter.end,
            rollup,
        )

    with sentry_sdk.start_span(
        op="discover.discover", description="top_events.transform_results"
    ) as span:
        span.set_data("result_count", len(result.get("data", [])))
        result = transform_data(result, translated_columns, snuba_filter, selected_columns)

        if "project" in selected_columns:
            translated_columns["project_id"] = "project"
        translated_groupby = [
            translated_columns.get(groupby, groupby) for groupby in snuba_filter.groupby
        ]

        issues = {}
        if "issue" in selected_columns:
            issues = Group.issues_mapping(
                set([event["issue.id"] for event in top_events["data"]]),
                params["project_id"],
                organization,
            )
        # so the result key is consistent
        translated_groupby.sort()

        results = {}
        # Using the top events add the order to the results
        for index, item in enumerate(top_events["data"]):
            result_key = create_result_key(item, translated_groupby, issues)
            results[result_key] = {"order": index, "data": []}
        for row in result["data"]:
            result_key = create_result_key(row, translated_groupby, issues)
            if result_key in results:
                results[result_key]["data"].append(row)
            else:
                logger.warning(
                    "discover.top-events.timeseries.key-mismatch",
                    extra={"result_key": result_key, "top_event_keys": list(results.keys())},
                )
        for key, item in six.iteritems(results):
            results[key] = SnubaTSResult(
                {
                    "data": zerofill(
                        item["data"], snuba_filter.start, snuba_filter.end, rollup, "time"
                    ),
                    "order": item["order"],
                },
                snuba_filter.start,
                snuba_filter.end,
                rollup,
            )

    return results


def get_id(result):
    if result:
        return result[1]


def get_facets(query, params, limit=10, referrer=None):
    """
    High-level API for getting 'facet map' results.

    Facets are high frequency tags and attribute results that
    can be used to further refine user queries. When many projects
    are requested sampling will be enabled to help keep response times low.

    query (str) Filter query string to create conditions from.
    params (Dict[str, str]) Filtering parameters with start, end, project_id, environment
    limit (int) The number of records to fetch.
    referrer (str|None) A referrer string to help locate the origin of this query.

    Returns Sequence[FacetResult]
    """
    with sentry_sdk.start_span(
        op="discover.discover", description="facets.filter_transform"
    ) as span:
        span.set_data("query", query)
        snuba_filter = get_filter(query, params)

        # Resolve the public aliases into the discover dataset names.
        snuba_filter, translated_columns = resolve_discover_aliases(snuba_filter)

    # Exclude tracing tags as they are noisy and generally not helpful.
    # TODO(markus): Tracing tags are no longer written but may still reside in DB.
    excluded_tags = ["tags_key", "NOT IN", ["trace", "trace.ctx", "trace.span", "project"]]

    # Sampling keys for multi-project results as we don't need accuracy
    # with that much data.
    sample = len(snuba_filter.filter_keys["project_id"]) > 2

    with sentry_sdk.start_span(op="discover.discover", description="facets.frequent_tags"):
        # Get the most frequent tag keys
        key_names = raw_query(
            aggregations=[["count", None, "count"]],
            start=snuba_filter.start,
            end=snuba_filter.end,
            conditions=snuba_filter.conditions,
            filter_keys=snuba_filter.filter_keys,
            orderby=["-count", "tags_key"],
            groupby="tags_key",
            having=[excluded_tags],
            dataset=Dataset.Discover,
            limit=limit,
            referrer=referrer,
            turbo=sample,
        )
        top_tags = [r["tags_key"] for r in key_names["data"]]
        if not top_tags:
            return []

    # TODO(mark) Make the sampling rate scale based on the result size and scaling factor in
    # sentry.options. To test the lowest acceptable sampling rate, we use 0.1 which
    # is equivalent to turbo. We don't use turbo though as we need to re-scale data, and
    # using turbo could cause results to be wrong if the value of turbo is changed in snuba.
    sampling_enabled = options.get("discover2.tags_facet_enable_sampling")
    sample_rate = 0.1 if (sampling_enabled and key_names["data"][0]["count"] > 10000) else None
    # Rescale the results if we're sampling
    multiplier = 1 / sample_rate if sample_rate is not None else 1

    fetch_projects = False
    if len(params.get("project_id", [])) > 1:
        if len(top_tags) == limit:
            top_tags.pop()
        fetch_projects = True

    results = []
    if fetch_projects:
        with sentry_sdk.start_span(op="discover.discover", description="facets.projects"):
            project_values = raw_query(
                aggregations=[["count", None, "count"]],
                start=snuba_filter.start,
                end=snuba_filter.end,
                conditions=snuba_filter.conditions,
                filter_keys=snuba_filter.filter_keys,
                groupby="project_id",
                orderby="-count",
                dataset=Dataset.Discover,
                referrer=referrer,
                sample=sample_rate,
                # Ensures Snuba will not apply FINAL
                turbo=sample_rate is not None,
            )
            results.extend(
                [
                    FacetResult("project", r["project_id"], int(r["count"]) * multiplier)
                    for r in project_values["data"]
                ]
            )

    # Get tag counts for our top tags. Fetching them individually
    # allows snuba to leverage promoted tags better and enables us to get
    # the value count we want.
    max_aggregate_tags = options.get("discover2.max_tags_to_combine")
    individual_tags = []
    aggregate_tags = []
    for i, tag in enumerate(top_tags):
        if tag == "environment":
            # Add here tags that you want to be individual
            individual_tags.append(tag)
        elif i >= len(top_tags) - max_aggregate_tags:
            aggregate_tags.append(tag)
        else:
            individual_tags.append(tag)

    with sentry_sdk.start_span(
        op="discover.discover", description="facets.individual_tags"
    ) as span:
        span.set_data("tag_count", len(individual_tags))
        for tag_name in individual_tags:
            tag = "tags[{}]".format(tag_name)
            tag_values = raw_query(
                aggregations=[["count", None, "count"]],
                conditions=snuba_filter.conditions,
                start=snuba_filter.start,
                end=snuba_filter.end,
                filter_keys=snuba_filter.filter_keys,
                orderby=["-count"],
                groupby=[tag],
                limit=TOP_VALUES_DEFAULT_LIMIT,
                dataset=Dataset.Discover,
                referrer=referrer,
                sample=sample_rate,
                # Ensures Snuba will not apply FINAL
                turbo=sample_rate is not None,
            )
            results.extend(
                [
                    FacetResult(tag_name, r[tag], int(r["count"]) * multiplier)
                    for r in tag_values["data"]
                ]
            )

    if aggregate_tags:
        with sentry_sdk.start_span(op="discover.discover", description="facets.aggregate_tags"):
            conditions = snuba_filter.conditions
            conditions.append(["tags_key", "IN", aggregate_tags])
            tag_values = raw_query(
                aggregations=[["count", None, "count"]],
                conditions=conditions,
                start=snuba_filter.start,
                end=snuba_filter.end,
                filter_keys=snuba_filter.filter_keys,
                orderby=["tags_key", "-count"],
                groupby=["tags_key", "tags_value"],
                dataset=Dataset.Discover,
                referrer=referrer,
                sample=sample_rate,
                # Ensures Snuba will not apply FINAL
                turbo=sample_rate is not None,
                limitby=[TOP_VALUES_DEFAULT_LIMIT, "tags_key"],
            )
            results.extend(
                [
                    FacetResult(r["tags_key"], r["tags_value"], int(r["count"]) * multiplier)
                    for r in tag_values["data"]
                ]
            )

    return results


HistogramParams = namedtuple(
    "HistogramParams", ["num_buckets", "bucket_size", "start_offset", "multiplier"]
)


def histogram_query(
    fields,
    user_query,
    params,
    num_buckets,
    precision=0,
    min_value=None,
    max_value=None,
    data_filter=None,
    referrer=None,
):
    """
    API for generating histograms for numeric columns.

    A multihistogram is possible only if the columns are all measurements.
    The resulting histograms will have their bins aligned.

    :param [str] fields: The list of fields for which you want to generate histograms for.
    :param str user_query: Filter query string to create conditions from.
    :param {str: str} params: Filtering parameters with start, end, project_id, environment
    :param int num_buckets: The number of buckets the histogram should contain.
    :param int precision: The number of decimal places to preserve, default 0.
    :param float min_value: The minimum value allowed to be in the histogram.
        If left unspecified, it is queried using `user_query` and `params`.
    :param float max_value: The maximum value allowed to be in the histogram.
        If left unspecified, it is queried using `user_query` and `params`.
    :param str data_filter: Indicate the filter strategy to be applied to the data.
    """

    multiplier = int(10 ** precision)
    if max_value is not None:
        # We want the specified max_value to be exclusive, and the queried max_value
        # to be inclusive. So we adjust the specified max_value using the multiplier.
        max_value -= 0.1 / multiplier
    min_value, max_value = find_histogram_min_max(
        fields, min_value, max_value, user_query, params, data_filter
    )

    key_column = None
    conditions = []
    if len(fields) > 1:
        key_column = "array_join(measurements_key)"
        key_alias = get_function_alias(key_column)
        measurements = []
        for f in fields:
            measurement = get_measurement_name(f)
            if measurement is None:
                raise InvalidSearchQuery(
                    "multihistogram expected all measurements, received: {}".format(f)
                )
            measurements.append(measurement)
        conditions.append([key_alias, "IN", measurements])

    histogram_params = find_histogram_params(num_buckets, min_value, max_value, multiplier)
    histogram_column = get_histogram_column(fields, key_column, histogram_params)
    histogram_alias = get_function_alias(histogram_column)

    if min_value is None or max_value is None:
        return normalize_histogram_results(fields, key_column, histogram_params, {"data": []})
    # make sure to bound the bins to get the desired range of results
    if min_value is not None:
        min_bin = histogram_params.start_offset
        conditions.append([histogram_alias, ">=", min_bin])
    if max_value is not None:
        max_bin = histogram_params.start_offset + histogram_params.bucket_size * num_buckets
        conditions.append([histogram_alias, "<=", max_bin])

    columns = [] if key_column is None else [key_column]
    results = query(
        selected_columns=columns + [histogram_column, "count()"],
        conditions=conditions,
        query=user_query,
        params=params,
        orderby=[histogram_alias],
        limit=len(fields) * num_buckets,
        referrer=referrer,
        functions_acl=["array_join", "histogram"],
    )

    return normalize_histogram_results(fields, key_column, histogram_params, results)


def get_histogram_column(fields, key_column, histogram_params):
    """
    Generate the histogram column string.

    :param [str] fields: The list of fields for which you want to generate the histograms for.
    :param str key_column: The column for the key name. This is only set when generating a
        multihistogram of measurement values. Otherwise, it should be `None`.
    :param HistogramParms histogram_params: The histogram parameters used.
    """

    field = fields[0] if key_column is None else "measurements_value"
    return "histogram({}, {:d}, {:d}, {:d})".format(
        field,
        histogram_params.bucket_size,
        histogram_params.start_offset,
        histogram_params.multiplier,
    )


def find_histogram_params(num_buckets, min_value, max_value, multiplier):
    """
    Compute the parameters to use for measurements histogram. Using the provided
    arguments, ensure that the generated histogram encapsolates the desired range.

    :param int num_buckets: The number of buckets the histogram should contain.
    :param float min_value: The minimum value allowed to be in the histogram inclusive.
    :param float max_value: The maximum value allowed to be in the histogram inclusive.
    :param int multipler: The multiplier we should use to preserve the desired precision.
    """

    scaled_min = 0 if min_value is None else multiplier * min_value
    scaled_max = 0 if max_value is None else multiplier * max_value

    # align the first bin with the minimum value
    start_offset = int(scaled_min)

    # finding the bounds might result in None if there isn't sufficient data
    if min_value is None or max_value is None:
        return HistogramParams(num_buckets, 1, start_offset, multiplier)

    bucket_size = nice_int((scaled_max - scaled_min) / float(num_buckets))

    if bucket_size == 0:
        bucket_size = 1

    # adjust the first bin to a nice value
    start_offset = int(scaled_min / bucket_size) * bucket_size

    # Sometimes the max value lies on the bucket boundary, and since the end
    # of the bucket is exclusive, it gets excluded. To account for that, we
    # increase the width of the buckets to cover the max value.
    if start_offset + num_buckets * bucket_size <= scaled_max:
        bucket_size = nice_int(bucket_size + 1)

    # compute the bin for max value and adjust the number of buckets accordingly
    # to minimize unnecessary empty bins at the tail
    last_bin = int((scaled_max - start_offset) / bucket_size) * bucket_size + start_offset
    num_buckets = (last_bin - start_offset) // bucket_size + 1

    return HistogramParams(num_buckets, bucket_size, start_offset, multiplier)


def find_histogram_min_max(fields, min_value, max_value, user_query, params, data_filter=None):
    """
    Find the min/max value of the specified measurements. If either min/max is already
    specified, it will be used and not queried for.

    :param [str] fields: The list of fields for which you want to generate the histograms for.
    :param float min_value: The minimum value allowed to be in the histogram.
        If left unspecified, it is queried using `user_query` and `params`.
    :param float max_value: The maximum value allowed to be in the histogram.
        If left unspecified, it is queried using `user_query` and `params`.
    :param str user_query: Filter query string to create conditions from.
    :param {str: str} params: Filtering parameters with start, end, project_id, environment
    :param str data_filter: Indicate the filter strategy to be applied to the data.
    """

    if min_value is not None and max_value is not None:
        return min_value, max_value

    min_columns = []
    max_columns = []
    quartiles = []
    for field in fields:
        if min_value is None:
            min_columns.append("min({})".format(field))
        if max_value is None:
            max_columns.append("max({})".format(field))
        if data_filter == "exclude_outliers":
            quartiles.append("percentile({}, 0.25)".format(field))
            quartiles.append("percentile({}, 0.75)".format(field))

    results = query(
        selected_columns=min_columns + max_columns + quartiles,
        query=user_query,
        params=params,
        limit=1,
        referrer="api.organization-events-histogram-min-max",
    )

    data = results.get("data")

    # there should be exactly 1 row in the results, but if something went wrong here,
    # we force the min/max to be None to coerce an empty histogram
    if data is None or len(data) != 1:
        return None, None

    row = data[0]

    if min_value is None:
        min_values = [row[get_function_alias(column)] for column in min_columns]
        min_values = list(filter(lambda v: v is not None, min_values))
        min_value = min(min_values) if min_values else None

    if max_value is None:
        max_values = [row[get_function_alias(column)] for column in max_columns]
        max_values = list(filter(lambda v: v is not None, max_values))
        max_value = max(max_values) if max_values else None

        fences = []
        if data_filter == "exclude_outliers":
            for field in fields:
                q1_alias = get_function_alias("percentile({}, 0.25)".format(field))
                q3_alias = get_function_alias("percentile({}, 0.75)".format(field))

                first_quartile = row[q1_alias]
                third_quartile = row[q3_alias]

                if (
                    first_quartile is None
                    or third_quartile is None
                    or math.isnan(first_quartile)
                    or math.isnan(third_quartile)
                ):
                    continue

                interquartile_range = abs(third_quartile - first_quartile)
                upper_outer_fence = third_quartile + 3 * interquartile_range
                fences.append(upper_outer_fence)

        max_fence_value = max(fences) if fences else None

        candidates = [max_fence_value, max_value]
        candidates = list(filter(lambda v: v is not None, candidates))
        max_value = min(candidates) if candidates else None

    return min_value, max_value


def normalize_histogram_results(fields, key_column, histogram_params, results):
    """
    Normalizes the histogram results by renaming the columns to key and bin
    and make sure to zerofill any missing values.

    :param [str] fields: The list of fields for which you want to generate the
        histograms for.
    :param str key_column: The column of the key name.
    :param HistogramParms histogram_params: The histogram parameters used.
    :param any results: The results from the histogram query that may be missing
        bins and needs to be normalized.
    """

    # `key_name` is only used when generating a multi histogram of measurement values.
    # It contains the name of the corresponding measurement for that row.
    key_name = None if key_column is None else get_function_alias(key_column)
    histogram_column = get_histogram_column(fields, key_column, histogram_params)
    bin_name = get_function_alias(histogram_column)

    # zerofill and rename the columns while making sure to adjust for precision
    bucket_maps = {field: {} for field in fields}
    for row in results["data"]:
        # Fall back to the first field name if there is no `key_name`,
        # otherwise, this is a measurement name and format it as such.
        key = fields[0] if key_name is None else "measurements.{}".format(row[key_name])
        # we expect the bin the be an integer, this is because all floating
        # point values are rounded during the calculation
        bucket = int(row[bin_name])
        # ignore unexpected keys
        if key in bucket_maps:
            bucket_maps[key][bucket] = row["count"]

    new_data = {field: [] for field in fields}
    for i in range(histogram_params.num_buckets):
        bucket = histogram_params.start_offset + histogram_params.bucket_size * i
        for field in fields:
            row = {
                "bin": bucket,
                "count": bucket_maps[field].get(bucket, 0),
            }
            # make sure to adjust for the precision if necessary
            if histogram_params.multiplier > 1:
                row["bin"] /= float(histogram_params.multiplier)
            new_data[field].append(row)

    return new_data

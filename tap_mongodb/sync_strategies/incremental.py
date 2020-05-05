#!/usr/bin/env python3
import copy
import time
import pymongo
import singer

from typing import Dict, Optional
from pymongo.collection import Collection
from singer import metadata, utils

import tap_mongodb.sync_strategies.common as common

LOGGER = singer.get_logger('tap_mongodb')


def update_bookmark(row: Dict, state: Dict, tap_stream_id: str, replication_key_name: str) -> None:
    """
    Updates replication key and type values in state bookmark
    Args:
        row: DB record
        state: dictionary of bookmarks
        tap_stream_id: stream ID
        replication_key_name: replication key
    """
    replication_key_value = row.get(replication_key_name)

    if replication_key_value:

        replication_key_type = replication_key_value.__class__.__name__

        replication_key_value_bookmark = common.class_to_string(replication_key_value, replication_key_type)

        state = singer.write_bookmark(state,
                                      tap_stream_id,
                                      'replication_key_value',
                                      replication_key_value_bookmark)

        singer.write_bookmark(state,
                              tap_stream_id,
                              'replication_key_type',
                              replication_key_type)


# pylint: disable=too-many-locals, too-many-statements
def sync_collection(collection: Collection,
                    stream: Dict,
                    state: Optional[Dict],
                    projection: Optional[str]
                    ):
    """
    Syncs the stream records incrementally
    Args:
        client: MongoDB client instance
        stream: stream dictionary
        state: state dictionary if exists
        projection: projection for querying if exists
    """
    tap_stream_id = stream['tap_stream_id']

    LOGGER.info('Starting incremental sync for %s', tap_stream_id)

    stream_metadata = metadata.to_map(stream['metadata']).get(())

    # before writing the table version to state, check if we had one to begin with
    first_run = singer.get_bookmark(state, stream['tap_stream_id'], 'version') is None

    # pick a new table version if last run wasn't interrupted
    if first_run:
        stream_version = int(time.time() * 1000)
    else:
        stream_version = singer.get_bookmark(state, stream['tap_stream_id'], 'version')

    state = singer.write_bookmark(state,
                                  stream['tap_stream_id'],
                                  'version',
                                  stream_version)

    activate_version_message = singer.ActivateVersionMessage(
        stream=common.calculate_destination_stream_name(stream),
        version=stream_version
    )

    # For the initial replication, emit an ACTIVATE_VERSION message
    # at the beginning so the records show up right away.
    if first_run:
        singer.write_message(activate_version_message)

    # get replication key, and bookmarked value/type
    stream_state = state.get('bookmarks', {}).get(tap_stream_id, {})

    replication_key_name = stream_metadata.get('replication-key')
    replication_key_value_bookmark = stream_state.get('replication_key_value')

    # write state message
    singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))

    # create query
    find_filter = {}

    if replication_key_value_bookmark:
        find_filter[replication_key_name] = {}
        find_filter[replication_key_name]['$gte'] = common.string_to_class(replication_key_value_bookmark,
                                                                           stream_state.get('replication_key_type'))

    # log query
    query_message = f'Querying {tap_stream_id} with: {dict(find=find_filter, projection=projection)}'

    LOGGER.info(query_message)

    # query collection
    schema = {"type": "object", "properties": {}}

    with collection.find(find_filter,
                         projection,
                         sort=[(replication_key_name, pymongo.ASCENDING)]) as cursor:
        rows_saved = 0
        time_extracted = utils.now()
        start_time = time.time()

        for row in cursor:
            schema_build_start_time = time.time()

            if common.row_to_schema(schema, row):
                singer.write_message(singer.SchemaMessage(
                    stream=common.calculate_destination_stream_name(stream),
                    schema=schema,
                    key_properties=['_id']))

                common.SCHEMA_COUNT[tap_stream_id] += 1

            common.SCHEMA_TIMES[tap_stream_id] += time.time() - schema_build_start_time

            record_message = common.row_to_singer_record(stream,
                                                         row,
                                                         stream_version,
                                                         time_extracted)

            singer.write_message(record_message)
            rows_saved += 1

            update_bookmark(row, state, tap_stream_id, replication_key_name)

            if rows_saved % common.UPDATE_BOOKMARK_PERIOD == 0:
                singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))

        common.COUNTS[tap_stream_id] += rows_saved
        common.TIMES[tap_stream_id] += time.time() - start_time

    singer.write_message(activate_version_message)

    LOGGER.info('Syncd %s records for %s', rows_saved, tap_stream_id)
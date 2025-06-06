"""
Handlers for video block.

StudentViewHandlers are handlers for video block instance.
StudioViewHandlers are handlers for video descriptor instance.
"""


import json
import logging
import math

from django.core.files.base import ContentFile
from django.utils.timezone import now
from edxval.api import create_external_video, create_or_update_video_transcript, delete_video_transcript
from opaque_keys.edx.locator import CourseLocator, LibraryLocatorV2
from webob import Response
from xblock.core import XBlock
from xblock.exceptions import JsonHandlerError

from xmodule.exceptions import NotFoundError
from xmodule.fields import RelativeTime
from openedx.core.djangoapps.content_libraries import api as lib_api

from .transcripts_utils import (
    Transcript,
    TranscriptException,
    TranscriptsGenerationException,
    clean_video_id,
    generate_sjson_for_all_speeds,
    get_html5_ids,
    get_or_create_sjson,
    get_transcript,
    get_transcript_from_contentstore,
    remove_subs_from_store,
    subs_filename,
    youtube_speed_dict
)

log = logging.getLogger(__name__)


# Disable no-member warning:
# pylint: disable=no-member

def to_boolean(value):
    """
    Convert a value from a GET or POST request parameter to a bool
    """
    if isinstance(value, bytes):
        value = value.decode('ascii', errors='replace')
    if isinstance(value, str):
        return value.lower() == 'true'
    else:
        return bool(value)


class VideoStudentViewHandlers:
    """
    Handlers for video block instance.
    """
    global_speed = None
    transcript_language = None

    def handle_ajax(self, dispatch, data):
        """
        Update values of xfields, that were changed by student.
        """
        accepted_keys = [
            'speed', 'auto_advance', 'saved_video_position', 'transcript_language',
            'transcript_download_format', 'youtube_is_available',
            'bumper_last_view_date', 'bumper_do_not_show_again'
        ]

        conversions = {
            'speed': json.loads,
            'auto_advance': json.loads,
            'saved_video_position': RelativeTime.isotime_to_timedelta,
            'youtube_is_available': json.loads,
            'bumper_last_view_date': to_boolean,
            'bumper_do_not_show_again': to_boolean,
        }

        if dispatch == 'save_user_state':
            for key in data:
                if key in accepted_keys:
                    if key in conversions:
                        value = conversions[key](data[key])
                    else:
                        value = data[key]

                    if key == 'bumper_last_view_date':
                        value = now()

                    if key == 'speed' and math.isnan(value):
                        message = f"Invalid speed value {value}, must be a float."
                        log.warning(message)
                        return json.dumps({'success': False, 'error': message})

                    setattr(self, key, value)

                    if key == 'speed':
                        self.global_speed = self.speed

            return json.dumps({'success': True})

        log.debug(f"GET {data}")
        log.debug(f"DISPATCH {dispatch}")

        raise NotFoundError('Unexpected dispatch type')

    def translation(self, youtube_id, transcripts):
        """
        This is called to get transcript file for specific language.

        youtube_id: str: must be one of youtube_ids or None if HTML video
        transcripts (dict): A dict with all transcripts and a sub.

        Logic flow:

        If youtube_id doesn't exist, we have a video in HTML5 mode. Otherwise,
        video in Youtube or Flash modes.

        if youtube:
            If english -> give back youtube_id subtitles:
                Return what we have in contentstore for given youtube_id.
            If non-english:
                a) extract youtube_id from srt file name.
                b) try to find sjson by youtube_id and return if successful.
                c) generate sjson from srt for all youtube speeds.
        if non-youtube:
            If english -> give back `sub` subtitles:
                Return what we have in contentstore for given subs_if that is stored in self.sub.
            If non-english:
                a) try to find previously generated sjson.
                b) otherwise generate sjson from srt and return it.

        Filenames naming:
            en: subs_videoid.srt.sjson
            non_en: uk_subs_videoid.srt.sjson

        Raises:
            NotFoundError if for 'en' subtitles no asset is uploaded.
            NotFoundError if youtube_id does not exist / invalid youtube_id
        """
        sub, other_lang = transcripts["sub"], transcripts["transcripts"]
        if youtube_id:
            # Youtube case:
            if self.transcript_language == 'en':
                return Transcript.asset(self.location, youtube_id).data

            youtube_ids = youtube_speed_dict(self)
            if youtube_id not in youtube_ids:
                log.info("Youtube_id %s does not exist", youtube_id)
                raise NotFoundError

            try:
                sjson_transcript = Transcript.asset(self.location, youtube_id, self.transcript_language).data
            except NotFoundError:
                log.info("Can't find content in storage for %s transcript: generating.", youtube_id)
                generate_sjson_for_all_speeds(
                    self,
                    other_lang[self.transcript_language],
                    {speed: youtube_id for youtube_id, speed in youtube_ids.items()},
                    self.transcript_language
                )
                sjson_transcript = Transcript.asset(self.location, youtube_id, self.transcript_language).data

            return sjson_transcript
        else:
            # HTML5 case
            if self.transcript_language == 'en':
                if '.srt' not in sub:  # not bumper case
                    return Transcript.asset(self.location, sub).data
                try:
                    return get_or_create_sjson(self, {'en': sub})
                except TranscriptException:
                    pass  # to raise NotFoundError and try to get data in get_static_transcript
            elif other_lang:
                return get_or_create_sjson(self, other_lang)

        raise NotFoundError

    def get_static_transcript(self, request, transcripts):
        """
        Courses that are imported with the --nostatic flag do not show
        transcripts/captions properly even if those captions are stored inside
        their static folder. This adds a last resort method of redirecting to
        the static asset path of the course if the transcript can't be found
        inside the contentstore and the course has the static_asset_path field
        set.

        transcripts (dict): A dict with all transcripts and a sub.
        """
        response = Response(status=404)
        # Only do redirect for English
        if not self.transcript_language == 'en':
            return response

        # If this video lives in library, the code below is not relevant and will error.
        if not isinstance(self.course_id, CourseLocator):
            return response

        video_id = request.GET.get('videoId', None)
        if video_id:
            transcript_name = video_id
        else:
            transcript_name = transcripts["sub"]

        if transcript_name:
            # Get the asset path for course
            asset_path = None
            course = self.runtime.modulestore.get_course(self.course_id)
            if course.static_asset_path:
                asset_path = course.static_asset_path
            else:
                # It seems static_asset_path is not set in any XMLModuleStore courses.
                asset_path = getattr(course, 'data_dir', '')

            if asset_path:
                response = Response(
                    status=307,
                    location='/static/{}/{}'.format(
                        asset_path,
                        subs_filename(transcript_name, self.transcript_language)
                    )
                )
        return response

    @XBlock.json_handler
    def publish_completion(self, data, dispatch):  # pylint: disable=unused-argument
        """
        Entry point for completion for student_view.

        Parameters:
            data: JSON dict:
                key: "completion"
                value: float in range [0.0, 1.0]

            dispatch: Ignored.
        Return value: JSON response (200 on success, 400 for malformed data)
        """
        completion_service = self.runtime.service(self, 'completion')
        if completion_service is None:
            raise JsonHandlerError(500, "No completion service found")
        if not completion_service.completion_tracking_enabled():
            raise JsonHandlerError(404, "Completion tracking is not enabled and API calls are unexpected")
        if not isinstance(data['completion'], (int, float)):
            message = "Invalid completion value {}. Must be a float in range [0.0, 1.0]"
            raise JsonHandlerError(400, message.format(data['completion']))
        if not 0.0 <= data['completion'] <= 1.0:
            message = "Invalid completion value {}. Must be in range [0.0, 1.0]"
            raise JsonHandlerError(400, message.format(data['completion']))
        self.runtime.publish(self, "completion", data)
        return {"result": "ok"}

    @staticmethod
    def make_transcript_http_response(content, filename, language, content_type, add_attachment_header=True):
        """
        Construct `Response` object.

        Arguments:
            content (unicode): transcript content
            filename (unicode): transcript filename
            language (unicode): transcript language
            mimetype (unicode): transcript content type
            add_attachment_header (bool): whether to add attachment header or not
        """
        headerlist = [
            ('Content-Language', language),
        ]

        if add_attachment_header:
            headerlist.append(
                (
                    'Content-Disposition',
                    f'attachment; filename="{filename}"'
                )
            )

        response = Response(
            content,
            headerlist=headerlist,
            charset='utf8'
        )
        response.content_type = content_type

        return response

    @XBlock.handler
    def transcript(self, request, dispatch):
        """
        Entry point for transcript handlers for student_view.

        Request GET contains:
            (optional) `videoId` for `translation` dispatch.
            `is_bumper=1` flag for bumper case.

        Dispatches, (HTTP GET):
            /translation/[language_id]
            /download
            /available_translations/

        Explanations:
            `download`: returns SRT or TXT file.
            `translation`: depends on HTTP methods:
                    Provide translation for requested language, SJSON format is sent back on success,
                    Proper language_id should be in url.
            `available_translations`:
                    Returns list of languages, for which transcript files exist.
                    For 'en' check if SJSON exists. For non-`en` check if SRT file exists.
        """
        is_bumper = request.GET.get('is_bumper', False)
        transcripts = self.get_transcripts_info(is_bumper)

        if dispatch.startswith('translation'):
            language = dispatch.replace('translation', '').strip('/')

            # Because scrapers hit video blocks, verify that a user exists.
            # use the _request attr to get the django request object.
            if not request._request.user:  # pylint: disable=protected-access
                log.info("Transcript: user must be logged or public view enabled to get transcript")
                return Response(status=403)

            if not language:
                log.info("Invalid /translation request: no language.")
                return Response(status=400)

            if language not in ['en'] + list(transcripts["transcripts"].keys()):
                log.info("Video: transcript facilities are not available for given language.")
                return Response(status=404)

            if language != self.transcript_language:
                self.transcript_language = language

            try:
                if is_bumper:
                    content, filename, mimetype = get_transcript_from_contentstore(
                        self,
                        self.transcript_language,
                        Transcript.SJSON,
                        transcripts
                    )
                else:
                    content, filename, mimetype = get_transcript(
                        self,
                        lang=self.transcript_language,
                        output_format=Transcript.SJSON,
                        youtube_id=request.GET.get('videoId'),
                    )

                response = self.make_transcript_http_response(
                    content,
                    filename,
                    self.transcript_language,
                    mimetype,
                    add_attachment_header=False
                )
            except NotFoundError as exc:
                edx_video_id = clean_video_id(self.edx_video_id)
                log.warning(
                    '[Translation Dispatch] %s: %s',
                    self.location,
                    exc if is_bumper else f'Transcript not found for {edx_video_id}, lang: {self.transcript_language}',
                )
                response = self.get_static_transcript(request, transcripts)

        elif dispatch == 'download':
            lang = request.GET.get('lang', None)

            try:
                content, filename, mimetype = get_transcript(self, lang, output_format=self.transcript_download_format)
            except NotFoundError:
                return Response(status=404)

            response = self.make_transcript_http_response(
                content,
                filename,
                self.transcript_language,
                mimetype
            )
        elif dispatch.startswith('available_translations'):
            available_translations = self.available_translations(
                transcripts,
                verify_assets=True,
                is_bumper=is_bumper
            )
            if available_translations:
                response = Response(json.dumps(available_translations))
                response.content_type = 'application/json'
            else:
                response = Response(status=404)
        else:  # unknown dispatch
            log.debug("Dispatch is not allowed")
            response = Response(status=404)

        return response

    @XBlock.handler
    def student_view_user_state(self, request, suffix=''):  # lint-amnesty, pylint: disable=unused-argument
        """
        Endpoint to get user-specific state, like current position and playback speed,
        without rendering the full student_view HTML. This is similar to student_view_state,
        but that one cannot contain user-specific info.
        """
        view_state = self.student_view_data()
        view_state.update({
            "saved_video_position": self.saved_video_position.total_seconds(),
            "speed": self.speed,
        })
        return Response(
            json.dumps(view_state),
            content_type='application/json',
            charset='UTF-8'
        )

    @XBlock.handler
    def yt_video_metadata(self, request, suffix=''):  # lint-amnesty, pylint: disable=unused-argument
        """
        Endpoint to get YouTube metadata.
        This handler is only used in the Learning-Core-based runtime. The old
        runtime uses a similar REST API that's not an XBlock handler.
        """
        from lms.djangoapps.courseware.views.views import load_metadata_from_youtube
        if not self.youtube_id_1_0:
            # TODO: more informational response to explain that yt_video_metadata not supported for non-youtube videos.
            return Response('{}', status=400)

        metadata, status_code = load_metadata_from_youtube(video_id=self.youtube_id_1_0, request=request)
        response = Response(json.dumps(metadata), status=status_code)
        response.content_type = 'application/json'
        return response


class VideoStudioViewHandlers:
    """
    Handlers for Studio view.
    """
    def validate_transcript_upload_data(self, data):
        """
        Validates video transcript file.
        Arguments:
            data: Transcript data to be validated.
        Returns:
            None or String
            If there is error returns error message otherwise None.
        """
        error = None
        _ = self.runtime.service(self, "i18n").ugettext
        # Validate the must have attributes - this error is unlikely to be faced by common users.
        must_have_attrs = ['edx_video_id', 'language_code', 'new_language_code']
        missing = [attr for attr in must_have_attrs if attr not in data]

        # Get available transcript languages.
        transcripts = self.get_transcripts_info()
        available_translations = self.available_translations(transcripts, verify_assets=True)

        if missing:
            error = _('The following parameters are required: {missing}.').format(missing=', '.join(missing))
        elif (
            data['language_code'] != data['new_language_code'] and data['new_language_code'] in available_translations
        ):
            error = _('A transcript with the "{language_code}" language code already exists.').format(
                language_code=data['new_language_code'],
            )
        elif 'file' not in data:
            error = _('A transcript file is required.')

        return error

    @XBlock.handler
    def studio_transcript(self, request, dispatch):
        """
        Entry point for Studio transcript handlers.

        Dispatches:
            /translation/[language_id] - language_id sould be in url.

        `translation` dispatch support following HTTP methods:
            `POST`:
                Upload srt file. Check possibility of generation of proper sjson files.
                For now, it works only for self.transcripts, not for `en`.
            `GET:
                Return filename from storage. SRT format is sent back on success. Filename should be in GET dict.

        We raise all exceptions right in Studio:
            NotFoundError:
                Video or asset was deleted from module/contentstore, but request came later.
                Seems impossible to be raised. block_render.py catches NotFoundErrors from here.

            /translation POST:
                TypeError:
                    Unjsonable filename or content.
                TranscriptsGenerationException, TranscriptException:
                    no SRT extension or not parse-able by PySRT
                UnicodeDecodeError: non-UTF8 uploaded file content encoding.
        """
        if dispatch.startswith('translation'):

            if request.method == 'POST':
                response = self._studio_transcript_upload(request)
            elif request.method == 'DELETE':
                response = self._studio_transcript_delete(request)
            elif request.method == 'GET':
                response = self._studio_transcript_get(request)
            else:
                # Any other HTTP method is not allowed.
                response = Response(status=404)

        else:  # unknown dispatch
            log.debug("Dispatch is not allowed")
            response = Response(status=404)

        return response

    def _save_transcript_field(self):
        """
        Save `transcripts` block field.
        """
        field = self.fields['transcripts']
        if self.transcripts:
            transcripts_copy = self.transcripts.copy()
            # Need to delete to overwrite, it's weird behavior,
            # but it only works like this.
            field.delete_from(self)
            field.write_to(self, transcripts_copy)
        else:
            field.delete_from(self)

    def _studio_transcript_upload(self, request):
        """
        Upload transcript. Used in "POST" method in `studio_transcript`
        """
        _ = self.runtime.service(self, "i18n").ugettext
        error = self.validate_transcript_upload_data(data=request.POST)
        if error:
            response = Response(json={'error': error}, status=400)
        else:
            edx_video_id = clean_video_id(request.POST['edx_video_id'])
            language_code = request.POST['language_code']
            new_language_code = request.POST['new_language_code']
            transcript_file = request.POST['file'].file

            is_library = isinstance(self.usage_key.context_key, LibraryLocatorV2)

            if is_library:
                filename = f'transcript-{new_language_code}.srt'
            else:
                if not edx_video_id:
                    # Back-populate the video ID for an external video.
                    # pylint: disable=attribute-defined-outside-init
                    self.edx_video_id = edx_video_id = create_external_video(display_name='external video')
                filename = f'{edx_video_id}-{new_language_code}.srt'

            try:
                # Convert SRT transcript into an SJSON format
                # and upload it to S3.
                content = transcript_file.read()
                payload = {
                    'edx_video_id': edx_video_id,
                    'language_code': new_language_code
                }
                if is_library:
                    # Save transcript as static asset in Learning Core if is a library component
                    filename = f"static/{filename}"
                    lib_api.add_library_block_static_asset_file(
                        self.usage_key,
                        filename,
                        content,
                    )
                else:
                    sjson_subs = Transcript.convert(
                        content=content.decode('utf-8'),
                        input_format=Transcript.SRT,
                        output_format=Transcript.SJSON
                    ).encode()
                    create_or_update_video_transcript(
                        video_id=edx_video_id,
                        language_code=language_code,
                        metadata={
                            'file_format': Transcript.SJSON,
                            'language_code': new_language_code
                        },
                        file_data=ContentFile(sjson_subs),
                    )

                # If a new transcript is added, then both new_language_code and
                # language_code fields will have the same value.
                if language_code != new_language_code:
                    self.transcripts.pop(language_code, None)
                self.transcripts[new_language_code] = filename

                if is_library:
                    self._save_transcript_field()
                response = Response(json.dumps(payload), status=201)
            except (TranscriptsGenerationException, UnicodeDecodeError):
                response = Response(
                    json={
                        'error': _(
                            'There is a problem with this transcript file. Try to upload a different file.'
                        )
                    },
                    status=400
                )
        return response

    def _studio_transcript_delete(self, request):
        """
        Delete transcript. Used in "DELETE" method in `studio_transcript`
        """
        request_data = request.json

        if 'lang' not in request_data or 'edx_video_id' not in request_data:
            return Response(status=400)

        language = request_data['lang']
        edx_video_id = clean_video_id(request_data['edx_video_id'])

        if edx_video_id:
            delete_video_transcript(video_id=edx_video_id, language_code=language)

        if isinstance(self.usage_key.context_key, LibraryLocatorV2):
            transcript_name = self.transcripts.pop(language, None)
            if transcript_name:
                # TODO: In the future, we need a proper XBlock API
                # like `self.static_assets.delete(...)` instead of coding
                # these runtime-specific/library-specific APIs.
                lib_api.delete_library_block_static_asset_file(
                    self.usage_key,
                    f"static/{transcript_name}",
                )
                self._save_transcript_field()
        else:
            if language == 'en':
                # remove any transcript file from content store for the video ids
                possible_sub_ids = [
                    self.sub,  # pylint: disable=access-member-before-definition
                    self.youtube_id_1_0
                ] + get_html5_ids(self.html5_sources)
                for sub_id in possible_sub_ids:
                    remove_subs_from_store(sub_id, self, language)

                # update metadata as `en` can also be present in `transcripts` field
                remove_subs_from_store(self.transcripts.pop(language, None), self, language)

                # also empty `sub` field
                self.sub = ''  # pylint: disable=attribute-defined-outside-init
            else:
                remove_subs_from_store(self.transcripts.pop(language, None), self, language)

        return Response(status=200)

    def _studio_transcript_get(self, request):
        """
        Get transcript. Used in "GET" method in `studio_transcript`
        """
        _ = self.runtime.service(self, "i18n").ugettext
        language = request.GET.get('language_code')
        if not language:
            return Response(json={'error': _('Language is required.')}, status=400)

        try:
            transcript_content, transcript_name, mime_type = get_transcript(
                video=self, lang=language, output_format=Transcript.SRT
            )
            response = Response(transcript_content, headerlist=[
                (
                    'Content-Disposition',
                    f'attachment; filename="{transcript_name}"'
                ),
                ('Content-Language', language),
                ('Content-Type', mime_type)
            ])
        except (UnicodeDecodeError, TranscriptsGenerationException, NotFoundError):
            response = Response(status=404)
        return response

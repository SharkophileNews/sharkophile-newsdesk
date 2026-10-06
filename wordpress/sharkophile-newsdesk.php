<?php
/**
 * Plugin Name: Sharkophile Newsdesk Support
 * Description: Companion to the automated newsdesk. Lets the newsdesk sign in with its application password on hosts that strip the Authorization header, makes SEO title/description fields writable over the REST API (Genesis, Yoast, Rank Math), shows the newsdesk's review notes to editors on the post screen, and outputs NewsArticle structured data on posts.
 * Version: 1.3.0
 * Requires at least: 5.6
 * Author: Sharkophile
 * License: GPL-2.0-or-later
 *
 * Install either way:
 *  - Plugins → Add New → Upload Plugin → choose sharkophile-newsdesk-plugin.zip → Activate, or
 *  - copy this file to wp-content/mu-plugins/ (loads automatically, nothing to activate).
 * When activated as a regular plugin it also drops a one-line loader into
 * wp-content/mu-plugins/ so its login fix runs before every other plugin.
 */

defined( 'ABSPATH' ) || exit;

// Safe if loaded twice (regular plugin + mu-plugin loader).
if ( defined( 'SHARKOPHILE_NEWSDESK_LOADED' ) ) {
	return;
}
define( 'SHARKOPHILE_NEWSDESK_LOADED', '1.3.0' );
define( 'SHARKOPHILE_NEWSDESK_FILE', __FILE__ );

/**
 * 0. Application-password login fixes.
 *
 *  a) Some Apache/FastCGI hosts (Bluehost, HostGator…) drop the standard
 *     "Authorization" header before PHP sees it. The newsdesk also sends the same
 *     Basic credentials in "X-Newsdesk-Authorization"; when PHP received no Basic
 *     credentials we copy them over so WordPress core checks the application
 *     password exactly as usual. This grants nothing by itself. HTTPS only.
 *  b) WordPress only checks application passwords once it knows the request is an
 *     API request (REST_REQUEST), which it decides late. If any plugin asks who the
 *     current user is earlier, the check is skipped for the whole request. We tell
 *     WordPress up front that /wp-json/ requests are API requests.
 */
if ( ! function_exists( 'sharkophile_newsdesk_is_rest_uri' ) ) {
	function sharkophile_newsdesk_is_rest_uri() {
		$uri    = isset( $_SERVER['REQUEST_URI'] ) ? (string) $_SERVER['REQUEST_URI'] : '';
		$prefix = function_exists( 'rest_get_url_prefix' ) ? rest_get_url_prefix() : 'wp-json';
		return false !== strpos( $uri, '/' . trim( $prefix, '/' ) . '/' ) || isset( $_GET['rest_route'] ); // phpcs:ignore WordPress.Security.NonceVerification
	}
}

if ( ! function_exists( 'sharkophile_newsdesk_header_present' ) ) {
	function sharkophile_newsdesk_header_present( $name ) {
		return ! empty( $_SERVER[ $name ] );
	}
}

if ( ! function_exists( 'sharkophile_newsdesk_populate_basic_auth' ) ) {
	function sharkophile_newsdesk_populate_basic_auth() {
		if ( isset( $_SERVER['PHP_AUTH_USER'] ) ) {
			return;
		}
		// Some setups expose the standard header only via the REDIRECT_ variable or apache_request_headers().
		$header = '';
		foreach ( array( 'HTTP_AUTHORIZATION', 'REDIRECT_HTTP_AUTHORIZATION', 'HTTP_X_NEWSDESK_AUTHORIZATION' ) as $key ) {
			if ( ! empty( $_SERVER[ $key ] ) ) {
				$header = (string) $_SERVER[ $key ];
				break;
			}
		}
		if ( '' === $header && function_exists( 'apache_request_headers' ) ) {
			$headers = array_change_key_case( (array) apache_request_headers(), CASE_LOWER );
			$header  = isset( $headers['authorization'] ) ? (string) $headers['authorization'] : ( isset( $headers['x-newsdesk-authorization'] ) ? (string) $headers['x-newsdesk-authorization'] : '' );
		}
		if ( '' === $header || 0 !== stripos( $header, 'basic ' ) || ! is_ssl() ) {
			return;
		}
		$decoded = base64_decode( substr( $header, 6 ), true );
		if ( false === $decoded || false === strpos( $decoded, ':' ) ) {
			return;
		}
		list( $user, $pass )     = explode( ':', $decoded, 2 );
		$_SERVER['PHP_AUTH_USER'] = $user;
		$_SERVER['PHP_AUTH_PW']   = $pass;
	}
}

sharkophile_newsdesk_populate_basic_auth();

// Run again at the moment WordPress decides who the user is (before core's check at priority 20).
add_filter( 'determine_current_user', static function ( $user_id ) {
	sharkophile_newsdesk_populate_basic_auth();
	return $user_id;
}, 1 );

add_filter( 'application_password_is_api_request', static function ( $is_api_request ) {
	return $is_api_request || sharkophile_newsdesk_is_rest_uri();
} );

// If another plugin already settled "nobody is logged in" before we loaded, let WordPress decide again.
if ( isset( $_SERVER['PHP_AUTH_USER'] ) && sharkophile_newsdesk_is_rest_uri() && ! empty( $GLOBALS['current_user'] )
	&& $GLOBALS['current_user'] instanceof WP_User && 0 === (int) $GLOBALS['current_user']->ID ) {
	$GLOBALS['current_user'] = null;
}

/**
 * Diagnostic: GET /wp-json/sharkophile-newsdesk/v1/auth-check
 * Reports yes/no facts about the current request — never any credential values.
 */
add_action( 'rest_api_init', static function () {
	register_rest_route( 'sharkophile-newsdesk/v1', '/auth-check', array(
		'methods'             => 'GET',
		'permission_callback' => '__return_true',
		'callback'            => static function () {
			global $wp_rest_application_password_status;
			$status = $wp_rest_application_password_status;
			return array(
				'plugin_version'               => SHARKOPHILE_NEWSDESK_LOADED,
				'loaded_early_via_mu_plugin'   => defined( 'SHARKOPHILE_NEWSDESK_VIA_MU' ),
				'authorization_header_arrived' => sharkophile_newsdesk_header_present( 'HTTP_AUTHORIZATION' ) || sharkophile_newsdesk_header_present( 'REDIRECT_HTTP_AUTHORIZATION' ),
				'newsdesk_header_arrived'      => sharkophile_newsdesk_header_present( 'HTTP_X_NEWSDESK_AUTHORIZATION' ),
				'credentials_reached_php'      => isset( $_SERVER['PHP_AUTH_USER'] ),
				'https'                        => is_ssl(),
				'app_passwords_available'      => function_exists( 'wp_is_application_passwords_available' ) && wp_is_application_passwords_available(),
				'logged_in_user_id'            => get_current_user_id(),
				'app_password_result'          => is_wp_error( $status ) ? $status->get_error_code() : ( $status ? 'accepted' : 'not_checked' ),
			);
		},
	) );
} );

/**
 * Early loader: on activation, add wp-content/mu-plugins/sharkophile-newsdesk-loader.php
 * so the login fix runs before all regular plugins. Removed on deactivation.
 */
if ( ! function_exists( 'sharkophile_newsdesk_loader_path' ) ) {
	function sharkophile_newsdesk_loader_path() {
		return ( defined( 'WPMU_PLUGIN_DIR' ) ? WPMU_PLUGIN_DIR : WP_CONTENT_DIR . '/mu-plugins' ) . '/sharkophile-newsdesk-loader.php';
	}
}

if ( ! function_exists( 'sharkophile_newsdesk_write_loader' ) ) {
	function sharkophile_newsdesk_write_loader() {
		$dir = dirname( sharkophile_newsdesk_loader_path() );
		if ( ! is_dir( $dir ) ) {
			wp_mkdir_p( $dir );
		}
		if ( ! is_dir( $dir ) || ! is_writable( $dir ) ) {
			return false;
		}
		$target = wp_normalize_path( SHARKOPHILE_NEWSDESK_FILE );
		$code   = "<?php\n// Loads Sharkophile Newsdesk Support early. Added on activation; removed on deactivation.\n"
			. 'if ( file_exists( ' . var_export( $target, true ) . " ) ) {\n"
			. "\tdefine( 'SHARKOPHILE_NEWSDESK_VIA_MU', true );\n"
			. "\tinclude_once " . var_export( $target, true ) . ";\n}\n";
		return false !== file_put_contents( sharkophile_newsdesk_loader_path(), $code ); // phpcs:ignore WordPress.WP.AlternativeFunctions
	}
}

register_activation_hook( __FILE__, 'sharkophile_newsdesk_write_loader' );

// Upgrading by "Replace current with uploaded" doesn't re-run activation, so add the loader on the next admin visit.
add_action( 'admin_init', static function () {
	if ( ! file_exists( sharkophile_newsdesk_loader_path() ) && current_user_can( 'activate_plugins' )
		&& 0 === strpos( wp_normalize_path( (string) realpath( SHARKOPHILE_NEWSDESK_FILE ) ), wp_normalize_path( (string) realpath( WP_PLUGIN_DIR ) ) ) ) {
		sharkophile_newsdesk_write_loader();
	}
} );

register_deactivation_hook( __FILE__, static function () {
	$path = sharkophile_newsdesk_loader_path();
	if ( file_exists( $path ) ) {
		unlink( $path ); // phpcs:ignore WordPress.WP.AlternativeFunctions
	}
} );

/**
 * 1. Expose meta keys to the REST API so the newsdesk can set them.
 *    Underscore-prefixed keys are "protected", so an auth callback is required.
 */
add_action( 'init', function () {
	$can_edit = static function () {
		return current_user_can( 'edit_posts' );
	};

	$text_keys = array(
		// Genesis Framework built-in SEO (used by Sharkophile's theme).
		'_genesis_title',
		'_genesis_description',
		// Yoast SEO and Rank Math, in case either is installed later.
		'_yoast_wpseo_title',
		'_yoast_wpseo_metadesc',
		'_yoast_wpseo_focuskw',
		'rank_math_title',
		'rank_math_description',
		'rank_math_focus_keyword',
		// Newsdesk bookkeeping.
		'_newsdesk_focus_keyword',
		'_newsdesk_run_id',
	);
	foreach ( $text_keys as $key ) {
		if ( registered_meta_key_exists( 'post', $key, 'post' ) || registered_meta_key_exists( 'post', $key ) ) {
			continue;
		}
		register_post_meta( 'post', $key, array(
			'type'              => 'string',
			'single'            => true,
			'show_in_rest'      => true,
			'auth_callback'     => $can_edit,
			'sanitize_callback' => 'sanitize_text_field',
		) );
	}

	$json_keys = array( '_newsdesk_sources', '_newsdesk_report' );
	foreach ( $json_keys as $key ) {
		register_post_meta( 'post', $key, array(
			'type'              => 'string',
			'single'            => true,
			'show_in_rest'      => true,
			'auth_callback'     => $can_edit,
			'sanitize_callback' => static function ( $value ) {
				$value = is_string( $value ) ? $value : '';
				return ( null === json_decode( $value ) ) ? '' : $value;
			},
		) );
	}
} );

/**
 * 2. Show the newsdesk's review notes on the edit screen.
 */
add_action( 'add_meta_boxes_post', function ( $post ) {
	if ( ! get_post_meta( $post->ID, '_newsdesk_report', true ) ) {
		return;
	}
	add_meta_box( 'sharkophile-newsdesk', '🦈 Newsdesk review notes', 'sharkophile_newsdesk_metabox', 'post', 'side', 'high' );
} );

if ( ! function_exists( 'sharkophile_newsdesk_metabox' ) ) {
	function sharkophile_newsdesk_metabox( $post ) {
		$report  = json_decode( (string) get_post_meta( $post->ID, '_newsdesk_report', true ), true );
		$sources = json_decode( (string) get_post_meta( $post->ID, '_newsdesk_sources', true ), true );
		if ( ! is_array( $report ) ) {
			echo '<p>No report.</p>';
			return;
		}
		$fact    = isset( $report['fact_check'] ) && is_array( $report['fact_check'] ) ? $report['fact_check'] : array();
		$seo     = isset( $report['seo'] ) && is_array( $report['seo'] ) ? $report['seo'] : array();
		$verdict = isset( $fact['verdict'] ) ? $fact['verdict'] : 'n/a';
		$labels  = array(
			'pass'         => '✅ Pass',
			'minor_issues' => '🟡 Minor issues',
			'major_issues' => '🔴 Major issues — verify before publishing',
		);

		if ( ! empty( $report['warnings'] ) && is_array( $report['warnings'] ) ) {
			echo '<div style="background:#fcf0e3;border-left:4px solid #dba617;padding:6px 10px;margin:6px 0 10px"><strong>Needs attention</strong><ul style="margin:4px 0 0 1em;list-style:disc">';
			foreach ( $report['warnings'] as $w ) {
				echo '<li>' . esc_html( (string) $w ) . '</li>';
			}
			echo '</ul></div>';
		}
		echo '<p><strong>Fact-check:</strong> ' . esc_html( isset( $labels[ $verdict ] ) ? $labels[ $verdict ] : $verdict ) . '</p>';
		if ( ! empty( $fact['issues'] ) && is_array( $fact['issues'] ) ) {
			echo '<ul style="margin-left:1em;list-style:disc">';
			foreach ( $fact['issues'] as $issue ) {
				printf(
					'<li><em>%s</em>: %s<br><small>Fix: %s</small></li>',
					esc_html( isset( $issue['severity'] ) ? $issue['severity'] : '' ),
					esc_html( isset( $issue['problem'] ) ? $issue['problem'] : '' ),
					esc_html( isset( $issue['fix'] ) ? $issue['fix'] : '' )
				);
			}
			echo '</ul>';
		}
		if ( isset( $seo['score'] ) ) {
			echo '<p><strong>SEO/style score:</strong> ' . (int) $seo['score'] . '/100</p>';
		}
		if ( ! empty( $seo['checks'] ) && is_array( $seo['checks'] ) ) {
			$fails = array_filter( $seo['checks'], static function ( $c ) {
				return empty( $c['ok'] );
			} );
			if ( $fails ) {
				echo '<ul style="margin-left:1em;list-style:disc">';
				foreach ( $fails as $c ) {
					printf( '<li>%s <small>%s</small></li>', esc_html( $c['name'] ), esc_html( isset( $c['detail'] ) ? $c['detail'] : '' ) );
				}
				echo '</ul>';
			}
		}
		if ( ! empty( $report['editor_notes'] ) ) {
			echo '<p><strong>Writer notes:</strong> ' . esc_html( $report['editor_notes'] ) . '</p>';
		}
		if ( is_array( $sources ) && $sources ) {
			echo '<p><strong>Sources</strong></p><ul style="margin-left:1em;list-style:disc">';
			foreach ( $sources as $s ) {
				if ( empty( $s['url'] ) ) {
					continue;
				}
				printf(
					'<li><a href="%s" target="_blank" rel="noopener">%s</a></li>',
					esc_url( $s['url'] ),
					esc_html( ! empty( $s['publisher'] ) ? $s['publisher'] : $s['url'] )
				);
			}
			echo '</ul>';
		}
		echo '<p><small>Delete the "_newsdesk_report" custom field to hide this box.</small></p>';
	}
}

/**
 * 3. NewsArticle structured data on single posts.
 *    Skipped when Yoast or Rank Math is active (they output their own schema),
 *    or when a theme/plugin returns false from the 'sharkophile_newsdesk_schema' filter.
 */
add_action( 'wp_head', function () {
	if ( ! is_singular( 'post' ) || defined( 'WPSEO_VERSION' ) || class_exists( 'RankMath' ) ) {
		return;
	}
	if ( ! apply_filters( 'sharkophile_newsdesk_schema', true ) ) {
		return;
	}
	$post = get_queried_object();
	if ( ! $post instanceof WP_Post ) {
		return;
	}

	$images = array();
	$thumb  = get_the_post_thumbnail_url( $post, 'full' );
	if ( $thumb ) {
		$images[] = $thumb;
	}
	$categories = get_the_category( $post->ID );
	$tag_terms  = get_the_tags( $post->ID );
	$sections   = $categories ? wp_list_pluck( $categories, 'name' ) : array();
	$tags       = ( $tag_terms && ! is_wp_error( $tag_terms ) ) ? wp_list_pluck( $tag_terms, 'name' ) : array();
	$author   = get_the_author_meta( 'display_name', $post->post_author );
	$logo     = get_site_icon_url( 512 );
	$headline = wp_strip_all_tags( get_the_title( $post ) );
	if ( function_exists( 'mb_substr' ) && mb_strlen( $headline ) > 110 ) {
		$headline = mb_substr( $headline, 0, 109 ) . '…';
	}

	$data = array(
		'@context'         => 'https://schema.org',
		'@type'            => 'NewsArticle',
		'headline'         => $headline,
		'description'      => wp_strip_all_tags( get_the_excerpt( $post ) ),
		'datePublished'    => get_the_date( 'c', $post ),
		'dateModified'     => get_the_modified_date( 'c', $post ),
		'mainEntityOfPage' => array( '@type' => 'WebPage', '@id' => get_permalink( $post ) ),
		'author'           => array(
			'@type' => ( 'Sharkophile Staff' === $author ) ? 'Organization' : 'Person',
			'name'  => $author,
			'url'   => get_author_posts_url( $post->post_author ),
		),
		'publisher'        => array_filter( array(
			'@type' => 'Organization',
			'name'  => get_bloginfo( 'name' ),
			'url'   => home_url( '/' ),
			'logo'  => $logo ? array( '@type' => 'ImageObject', 'url' => $logo ) : null,
		) ),
	);
	if ( $images ) {
		$data['image'] = $images;
	}
	if ( $sections ) {
		$data['articleSection'] = array_values( $sections );
	}
	if ( $tags ) {
		$data['keywords'] = implode( ', ', $tags );
	}

	echo "\n<script type=\"application/ld+json\">" . wp_json_encode( $data, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE ) . "</script>\n";
} );

<?php
/**
 * Plugin Name: Sharkophile Newsdesk Support
 * Description: Companion to the automated newsdesk. Lets the newsdesk sign in with its application password on hosts that strip the Authorization header, makes SEO title/description fields writable over the REST API (Genesis, Yoast, Rank Math), shows the newsdesk's review notes to editors on the post screen, and outputs NewsArticle structured data on posts.
 * Version: 1.1.0
 * Requires at least: 5.6
 * Author: Sharkophile
 * License: GPL-2.0-or-later
 *
 * Install either way:
 *  - Plugins → Add New → Upload Plugin → choose sharkophile-newsdesk-plugin.zip → Activate, or
 *  - copy this file to wp-content/mu-plugins/ (loads automatically, nothing to activate).
 */

defined( 'ABSPATH' ) || exit;

// Safe if installed both as a regular plugin and as a must-use plugin.
if ( defined( 'SHARKOPHILE_NEWSDESK_LOADED' ) ) {
	return;
}
define( 'SHARKOPHILE_NEWSDESK_LOADED', '1.1.0' );

/**
 * 0. Authorization header fallback.
 *    Some Apache/FastCGI hosts (Bluehost, HostGator…) drop the standard
 *    "Authorization" header before PHP sees it, so WordPress can't check the
 *    application password. The newsdesk also sends the same Basic credentials in
 *    "X-Newsdesk-Authorization"; if PHP received no Basic credentials, copy them
 *    over so WordPress core validates them exactly as usual (application
 *    passwords only — this grants nothing by itself). HTTPS only.
 */
( static function () {
	if ( isset( $_SERVER['PHP_AUTH_USER'] ) || ! empty( $_SERVER['HTTP_AUTHORIZATION'] ) || ! empty( $_SERVER['REDIRECT_HTTP_AUTHORIZATION'] ) ) {
		return;
	}
	if ( empty( $_SERVER['HTTP_X_NEWSDESK_AUTHORIZATION'] ) || ! is_ssl() ) {
		return;
	}
	$header = (string) $_SERVER['HTTP_X_NEWSDESK_AUTHORIZATION'];
	if ( 0 !== stripos( $header, 'basic ' ) ) {
		return;
	}
	$decoded = base64_decode( substr( $header, 6 ), true );
	if ( false === $decoded || false === strpos( $decoded, ':' ) ) {
		return;
	}
	list( $user, $pass )     = explode( ':', $decoded, 2 );
	$_SERVER['PHP_AUTH_USER'] = $user;
	$_SERVER['PHP_AUTH_PW']   = $pass;
} )();

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

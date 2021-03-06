//
//  input_model.h
//  qtip
//
//  Created by Ben Langmead on 9/15/15.
//  Copyright (c) 2015 JHU. All rights reserved.
//

#ifndef __qtip__input_model__
#define __qtip__input_model__

#include <stdio.h>
#include <algorithm>
#include "ds.h"
#include "template.h"
#include "rnglib.hpp"

/**
 * Encapsulates the input model so we can simulate reads similar to the input
 * reads/alignments.
 */
class InputModelUnpaired {
	
public:
	
	InputModelUnpaired(
		const EList<TemplateUnpaired>& ts,
		size_t n,
		float fraction_even,
		float low_score_bias) :
		ts_(ts),
		n_(n),
		fraction_even_(fraction_even),
		low_score_bias_(low_score_bias)
	{
		fraglen_avg_ = 0.0f;
		fraglen_max_ = 0;
		for(size_t i = 0; i < ts.size(); i++) {
			fraglen_avg_ += ((float)ts[i].reflen() / ts.size());
			fraglen_max_ = std::max(fraglen_max_, (size_t)ts[i].reflen());
		}
	}
	
	/**
	 * Draw a random unpaired template.
	 *
	 * TODO: allow the draw to be somehow weighted toward lower scores.
	 */
	const TemplateUnpaired& draw() const {
		assert(!empty());
		size_t rn = std::min((size_t)(r4_uni_01() * ts_.size()), ts_.size()-1);
		assert(rn < ts_.size());
		return ts_[rn];
	}
	
	/**
	 * Return true iff no templates were added.
	 */
	bool empty() const {
		return ts_.empty();
	}
	
	/**
	 * Return the number of unpaired input models encountered.
	 * TODO: if we subsample prior to initializing ts_, need to update this
	 * function.
	 */
	size_t num_added() const {
		return n_;
	}

	/**
	 * Return average length of all reads.
	 */
	float avg_len() const {
		return fraglen_avg_;
	}

	/**
	 * Return maximum length of any unpaired template.
	 */
	size_t max_len() const {
		return fraglen_max_;
	}

protected:
	
	const EList<TemplateUnpaired>& ts_;
	float fraglen_avg_;
	size_t n_;
	size_t fraglen_max_;
	float fraction_even_; // unused
	float low_score_bias_; // unused
};

class InputModelPaired {
	
public:
	
	InputModelPaired(
		const EList<TemplatePaired>& ts,
		size_t n,
		float fraction_even,
		float low_score_bias) :
		ts_(ts),
		n_(n),
		fraction_even_(fraction_even),
		low_score_bias_(low_score_bias)
	{
		fraglen_avg_ = 0.0f;
		fraglen_max_ = 0;
		for(size_t i = 0; i < ts.size(); i++) {
			fraglen_avg_ += ((float)ts[i].fraglen_ / ts.size());
			fraglen_max_ = std::max(fraglen_max_, ts[i].fraglen_);
		}
	}
	
	/**
	 * Draw a random paired template.
	 *
	 * TODO: allow the draw to be somehow weighted toward lower scores.
	 */
	const TemplatePaired& draw() const {
		assert(!empty());
		size_t rn = std::min((size_t)(r4_uni_01() * ts_.size()), ts_.size()-1);
		return ts_[rn];
	}

	/**
	 * Return true iff no templates were added.
	 */
	bool empty() const {
		return ts_.empty();
	}

	/**
	 * Return the number of unpaired input models encountered.
	 * TODO: if we subsample prior to initializing ts_, need to update this
	 * function.
	 */
	size_t num_added() const {
		return n_;
	}
	
	/**
	 * Return average length of all fragments.
	 */
	float avg_len() const {
		return fraglen_avg_;
	}

	/**
	 * Return maximum length of any unpaired template.
	 */
	size_t max_len() const {
		return fraglen_max_;
	}

protected:
	
	const EList<TemplatePaired>& ts_;
	float fraglen_avg_;
	size_t n_;
	size_t fraglen_max_;
	float fraction_even_; // unused
	float low_score_bias_; // unused
};

#endif /* defined(__qtip__input_model__) */
